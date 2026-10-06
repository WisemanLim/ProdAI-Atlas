package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"atlas-manager/internal/bundle"
)

// Values 는 atlas-values.yaml 이다 — 대화형 마법사와 비대화형(--values)이 같은 결과를 낸다 (FR-PLT-PKG-08).
type Values struct {
	Mode        string `yaml:"mode"`         // compose | local | kubernetes
	Dir         string `yaml:"dir"`          // 설치 디렉터리
	Project     string `yaml:"project"`      // compose 프로젝트명
	Registry    string `yaml:"registry"`     // 이미지 레지스트리 (에어갭: 사설)
	Version     string `yaml:"version"`      // 설치 버전
	ManagerPort int    `yaml:"manager_port"` // 콘솔·API 포트
	Apply       bool   `yaml:"apply"`        // 파일 생성 후 바로 기동
}

func (v *Values) Defaults() {
	if v.Mode == "" {
		v.Mode = "compose"
	}
	if v.Dir == "" {
		v.Dir = "./atlas"
	}
	if v.Project == "" {
		v.Project = "atlas"
	}
	if v.Registry == "" {
		v.Registry = "ghcr.io/prodai"
	}
	if v.Version == "" {
		v.Version = bundle.Version()
	}
	if v.ManagerPort == 0 {
		v.ManagerPort = 8080
	}
}

func LoadValues(path string) (Values, error) {
	var v Values
	b, err := os.ReadFile(path)
	if err != nil {
		return v, err
	}
	if err := yaml.Unmarshal(b, &v); err != nil {
		return v, err
	}
	v.Defaults()
	return v, nil
}

// Check 는 사전 점검 결과 한 줄이다.
type Check struct {
	Name, Detail string
	OK           bool
}

// Preflight 는 Docker·Compose v2·데몬·자원·포트를 점검한다.
func Preflight(v Values) []Check {
	var out []Check
	add := func(name string, ok bool, detail string) { out = append(out, Check{name, detail, ok}) }
	_, err := exec.LookPath("docker")
	add("docker", err == nil, "PATH 에 docker")
	ver, err := exec.Command("docker", "compose", "version", "--short").Output()
	add("compose v2", err == nil, strings.TrimSpace(string(ver)))
	info, err := exec.Command("docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}").Output()
	if err == nil {
		var cpu int
		var mem int64
		fmt.Sscanf(string(info), "%d %d", &cpu, &mem)
		add("자원", cpu >= 2 && mem >= 4<<30, fmt.Sprintf("CPU %d · RAM %dGiB (최소 2·4, 권장 4·8)", cpu, mem>>30))
	} else {
		add("docker 데몬", false, "docker info 실패")
	}
	l, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", v.ManagerPort))
	if err == nil {
		l.Close()
	}
	add(fmt.Sprintf("포트 %d", v.ManagerPort), err == nil, "콘솔·API")
	return out
}

// Install 은 번들 파일과 .env·atlas-values.yaml 을 쓰고, Apply 면 기동한다.
func Install(ctx context.Context, v Values, w io.Writer) error {
	switch v.Mode {
	case "local":
		fmt.Fprintln(w, "local 모드 (개발자·데모): 1) atlas-manager 단일 바이너리 실행 — DB_ENGINE=sqlite DATABASE_URL=file:./atlas.db ./atlas-manager")
		fmt.Fprintln(w, "                          2) uv tool install prodai-agents && atlas-agent serve --unit units.al_plan --port 8000")
		fmt.Fprintln(w, "로컬 프로세스 드라이버(감독·자동 포트, FR-PLT-DEP-01)는 후속 — 지금은 수동 실행 안내만 제공")
		return nil
	case "kubernetes":
		return fmt.Errorf("kubernetes 모드(Helm 차트·Operator, PRD §11.20)는 후속 릴리스")
	case "compose":
	default:
		return fmt.Errorf("mode: compose | local | kubernetes")
	}
	if err := os.MkdirAll(v.Dir, 0o755); err != nil {
		return err
	}
	written, err := bundle.WriteTo(v.Dir, false)
	if err != nil {
		return err
	}
	fmt.Fprintf(w, "번들 v%s → %s (%d 파일)\n", bundle.Version(), v.Dir, len(written))
	envPath := filepath.Join(v.Dir, ".env")
	if _, err := os.Stat(envPath); err != nil {
		example, err := os.ReadFile(filepath.Join(v.Dir, ".env.example"))
		if err != nil {
			return err
		}
		env := RenderEnv(string(example), map[string]string{"ATLAS_REGISTRY": v.Registry, "ATLAS_VERSION": v.Version,
			"MANAGER_PORT": fmt.Sprint(v.ManagerPort)})
		if err := os.WriteFile(envPath, []byte(env), 0o600); err != nil {
			return err
		}
		fmt.Fprintln(w, ".env 생성 — 시크릿 무작위 (권한 600)")
	} else {
		fmt.Fprintln(w, ".env 보존 (기존 설치)")
	}
	vb, _ := yaml.Marshal(v)
	_ = os.WriteFile(filepath.Join(v.Dir, "atlas-values.yaml"), vb, 0o644)
	if !v.Apply {
		fmt.Fprintf(w, "기동: cd %s && docker compose -p %s --env-file .env -f compose.yaml up -d  (또는 atlas install --apply)\n", v.Dir, v.Project)
		return nil
	}
	if err := compose(ctx, v, w, "up", "-d"); err != nil {
		return err
	}
	return WaitHealthy(ctx, fmt.Sprintf("http://127.0.0.1:%d/health", v.ManagerPort), 4*time.Minute, w)
}

func compose(ctx context.Context, v Values, w io.Writer, args ...string) error {
	base := []string{"compose", "-p", v.Project, "--env-file", ".env", "-f", "compose.yaml"}
	cmd := exec.CommandContext(ctx, "docker", append(base, args...)...)
	cmd.Dir, cmd.Stdout, cmd.Stderr = v.Dir, w, w
	return cmd.Run()
}

// WaitHealthy 는 Manager /health 가 200 이 될 때까지 기다린다.
func WaitHealthy(ctx context.Context, url string, timeout time.Duration, w io.Writer) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		res, err := http.Get(url)
		if err == nil && res.StatusCode == 200 {
			b, _ := io.ReadAll(res.Body)
			res.Body.Close()
			fmt.Fprintf(w, "healthy: %s\n", bytes.TrimSpace(b))
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
	return fmt.Errorf("%s 이 %s 안에 healthy 가 되지 않음", url, timeout)
}

// Backup 은 PostgreSQL 전체 덤프를 dir/backups 에 남긴다 (업그레이드 전 필수, FR-PLT-REL-08).
func Backup(ctx context.Context, v Values, w io.Writer) (string, error) {
	env, err := ReadEnv(filepath.Join(v.Dir, ".env"))
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Join(v.Dir, "backups"), 0o700); err != nil {
		return "", err
	}
	out := filepath.Join(v.Dir, "backups", fmt.Sprintf("atlas-%s-%s.sql", env["ATLAS_VERSION"], time.Now().UTC().Format("20060102T150405Z")))
	f, err := os.OpenFile(out, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return "", err
	}
	defer f.Close()
	cmd := exec.CommandContext(ctx, "docker", "compose", "-p", v.Project, "--env-file", ".env", "-f", "compose.yaml",
		"exec", "-T", "db", "pg_dump", "-U", env["DB_USER"], "-d", env["DB_NAME"], "--clean", "--if-exists")
	cmd.Dir, cmd.Stdout, cmd.Stderr = v.Dir, f, w
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("pg_dump 실패: %w", err)
	}
	st, _ := f.Stat()
	fmt.Fprintf(w, "백업: %s (%d bytes)\n", out, st.Size())
	return out, nil
}

// Running 은 기동 중인 구성 요소 버전이다 (Manager /health · /svc/gw/health · /svc/alrun/readyz).
func Running(port int) map[string]string {
	get := func(path string, keys ...string) map[string]string {
		res, err := http.Get(fmt.Sprintf("http://127.0.0.1:%d%s", port, path))
		if err != nil {
			return nil
		}
		defer res.Body.Close()
		var m map[string]any
		_ = json.NewDecoder(res.Body).Decode(&m)
		out := map[string]string{}
		for _, k := range keys {
			if s, ok := m[k].(string); ok {
				out[k] = s
			}
		}
		return out
	}
	r := map[string]string{}
	if m := get("/health", "version"); m != nil {
		r["manager"] = m["version"]
	}
	if m := get("/svc/gw/health", "version"); m != nil {
		r["gateway"] = m["version"]
	}
	if m := get("/svc/alrun/readyz", "agent_core"); m != nil {
		r["agentCore"] = m["agent_core"]
	}
	return r
}

// PlanUpgrade 는 호환성 매트릭스로 업그레이드 가능 여부를 판정한다 (atlas upgrade --dry-run).
func PlanUpgrade(compat map[string]any, current, target string, running map[string]string) ([]string, error) {
	vers, _ := compat["versions"].(map[string]any)
	entry, ok := vers[target].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("compat.json 에 %s 없음", target)
	}
	cv, err := ParseVer(current)
	if err != nil {
		return nil, err
	}
	tv, err := ParseVer(target)
	if err != nil {
		return nil, err
	}
	if Cmp(tv, cv) < 0 {
		return nil, fmt.Errorf("다운그레이드 %s → %s 거부 (전진 전용 마이그레이션)", current, target)
	}
	var plan []string
	if from, _ := entry["upgradeFrom"].(string); from != "" {
		if ok, _ := Satisfies(current, from); !ok {
			return nil, fmt.Errorf("%s 에서 %s 로 직접 업그레이드 불가 (허용: %s)", current, target, from)
		}
	}
	if core := running["agentCore"]; core != "" {
		rng, _ := entry["agentCore"].(string)
		ok, _ := Satisfies(core, rng)
		if !ok {
			return nil, fmt.Errorf("실행 중 에이전트 코어 %s 가 %s 의 지원 범위 %s 밖 — 에이전트 먼저 갱신", core, target, rng)
		}
		plan = append(plan, fmt.Sprintf("에이전트 코어 %s ∈ %s — N-1 공존 가능", core, rng))
	}
	plan = append(plan, fmt.Sprintf("마이그레이션: manager goose %v · runtime alembic %v (전진 전용, 기동 시 자동 적용)",
		entry["migrations"].(map[string]any)["manager"], entry["migrations"].(map[string]any)["runtime"]),
		"1) atlas backup  2) .env ATLAS_VERSION="+target+"  3) docker compose pull  4) up -d  5) /health 확인")
	return plan, nil
}

// Upgrade 는 백업 → 버전 교체 → pull·재기동 → 헬스 확인이다.
func Upgrade(ctx context.Context, v Values, target string, w io.Writer) error {
	if _, err := Backup(ctx, v, w); err != nil {
		return err
	}
	if err := SetEnv(filepath.Join(v.Dir, ".env"), "ATLAS_VERSION", target); err != nil {
		return err
	}
	if err := compose(ctx, v, w, "pull", "--ignore-pull-failures"); err != nil {
		fmt.Fprintln(w, "pull 실패 — 로컬 이미지로 계속")
	}
	if err := compose(ctx, v, w, "up", "-d"); err != nil {
		return err
	}
	return WaitHealthy(ctx, fmt.Sprintf("http://127.0.0.1:%d/health", v.ManagerPort), 4*time.Minute, w)
}
