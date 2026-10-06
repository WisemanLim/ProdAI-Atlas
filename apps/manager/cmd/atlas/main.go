// atlas — ProdAI Atlas CLI (FR-PLT-PKG-03·08 · PLT-REL-08): install(마법사) · backup · upgrade · version.
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"time"

	"github.com/charmbracelet/huh"
	"golang.org/x/term"

	"atlas-manager/internal/bundle"
	"atlas-manager/internal/cli"
)

var version = "0.0.0-dev"

func usage() {
	fmt.Fprintf(os.Stderr, `atlas %s — ProdAI Atlas CLI
  atlas install [--values atlas-values.yaml] [--dir DIR] [--apply] [--yes]   설치 마법사 (compose · local · kubernetes)
  atlas backup  [--dir DIR]                                                   PostgreSQL 백업
  atlas upgrade --to X.Y.Z [--dir DIR] [--dry-run] [--compat compat.json]     호환성 검사 후 업그레이드
  atlas login   [--manager URL]                                              SSO 로그인 (브라우저 장치 코드 승인)
  atlas whoami  [--manager URL]                                              현재 주체·조직
  atlas version
`, version)
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	var err error
	switch os.Args[1] {
	case "version":
		fmt.Printf("atlas %s (bundle %s)\n", version, bundle.Version())
	case "install":
		err = install(ctx, os.Args[2:])
	case "backup", "upgrade":
		err = maintain(ctx, os.Args[1], os.Args[2:])
	case "login", "whoami":
		err = account(ctx, os.Args[1], os.Args[2:])
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "오류:", err)
		os.Exit(1)
	}
}

func install(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("install", flag.ExitOnError)
	values := fs.String("values", "", "atlas-values.yaml (비대화형)")
	dir := fs.String("dir", "", "설치 디렉터리")
	apply := fs.Bool("apply", false, "파일 생성 후 기동")
	yes := fs.Bool("yes", false, "질문 없이 기본값")
	_ = fs.Parse(args)
	var v cli.Values
	var err error
	if *values != "" {
		if v, err = cli.LoadValues(*values); err != nil {
			return err
		}
	} else {
		v.Defaults()
		if !*yes && term.IsTerminal(int(os.Stdin.Fd())) {
			port := strconv.Itoa(v.ManagerPort)
			form := huh.NewForm(huh.NewGroup(
				huh.NewSelect[string]().Title("설치 모드").Options(
					huh.NewOption("compose — 단일 서버 (권장)", "compose"), huh.NewOption("local — 개발자·데모", "local"),
					huh.NewOption("kubernetes — Helm·Operator (후속)", "kubernetes")).Value(&v.Mode),
				huh.NewInput().Title("설치 디렉터리").Value(&v.Dir),
				huh.NewInput().Title("이미지 레지스트리 (에어갭: 사설 레지스트리)").Value(&v.Registry),
				huh.NewInput().Title("콘솔·API 포트").Value(&port),
				huh.NewConfirm().Title("파일 생성 후 바로 기동할까요?").Value(&v.Apply),
			))
			if err := form.Run(); err != nil {
				return err
			}
			if p, err := strconv.Atoi(port); err == nil {
				v.ManagerPort = p
			}
		}
	}
	if *dir != "" {
		v.Dir = *dir
	}
	if *apply {
		v.Apply = true
	}
	if v.Mode == "compose" {
		fmt.Println("사전 점검")
		for _, c := range cli.Preflight(v) {
			mark := "OK  "
			if !c.OK {
				mark = "WARN"
			}
			fmt.Printf("  [%s] %-12s %s\n", mark, c.Name, c.Detail)
		}
	}
	return cli.Install(ctx, v, os.Stdout)
}

func maintain(ctx context.Context, cmd string, args []string) error {
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	dir := fs.String("dir", "./atlas", "설치 디렉터리")
	to := fs.String("to", "", "대상 버전")
	dry := fs.Bool("dry-run", false, "검사만")
	compatPath := fs.String("compat", "", "compat.json (기본: 내장)")
	_ = fs.Parse(args)
	v, err := cli.LoadValues(*dir + "/atlas-values.yaml")
	if err != nil {
		v = cli.Values{Dir: *dir}
		v.Defaults()
	}
	v.Dir = *dir
	if cmd == "backup" {
		_, err := cli.Backup(ctx, v, os.Stdout)
		return err
	}
	if *to == "" {
		return fmt.Errorf("--to 필요")
	}
	compat, err := bundle.Compat()
	if *compatPath != "" {
		compat, err = cli.LoadCompat(*compatPath)
	}
	if err != nil {
		return err
	}
	env, err := cli.ReadEnv(*dir + "/.env")
	if err != nil {
		return err
	}
	running := cli.Running(v.ManagerPort)
	fmt.Printf("현재 %s · 실행 중 %v → 대상 %s\n", env["ATLAS_VERSION"], running, *to)
	plan, err := cli.PlanUpgrade(compat, env["ATLAS_VERSION"], *to, running)
	if err != nil {
		return err
	}
	for _, p := range plan {
		fmt.Println("  -", p)
	}
	if *dry {
		fmt.Println("dry-run: 변경 없음")
		return nil
	}
	return cli.Upgrade(ctx, v, *to, os.Stdout)
}

func account(ctx context.Context, cmd string, args []string) error {
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	manager := fs.String("manager", envOr("ATLAS_MANAGER_URL", "http://127.0.0.1:8080"), "Manager URL")
	_ = fs.Parse(args)
	c := &http.Client{Timeout: 15 * time.Second}
	if cmd == "login" {
		cr, err := cli.DeviceLogin(ctx, c, *manager, func(u, code string) {
			fmt.Printf("브라우저에서 승인하세요: %s\n  코드: %s\n", u, code)
		})
		if err != nil {
			return err
		}
		if err := cli.SaveCredentials(cr); err != nil {
			return err
		}
		fmt.Println("로그인 완료 →", cli.CredentialsPath())
		return nil
	}
	tok, err := cli.Token(ctx, c, *manager)
	if err != nil {
		return err
	}
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(*manager, "/")+"/api/v1/auth/me", nil)
	req.Header.Set("Authorization", "Bearer "+tok)
	res, err := c.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	b, _ := io.ReadAll(res.Body)
	if res.StatusCode != http.StatusOK {
		return fmt.Errorf("whoami: %d %s", res.StatusCode, b)
	}
	fmt.Println(string(b))
	return nil
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
