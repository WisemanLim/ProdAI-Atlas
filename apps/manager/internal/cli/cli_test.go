package cli

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

func TestSemver(t *testing.T) {
	cases := []struct {
		v, c string
		want bool
	}{{"0.2.0", ">=0.2.0 <1.0.0", true}, {"0.1.9", ">=0.2.0 <1.0.0", false}, {"1.0.0", ">=0.2.0 <1.0.0", false},
		{"0.3.0-beta.1", ">=0.2.0", true}, {"v0.2.1", "=0.2.1", true}}
	for _, c := range cases {
		if got, err := Satisfies(c.v, c.c); err != nil || got != c.want {
			t.Errorf("%s %s = %v %v", c.v, c.c, got, err)
		}
	}
	a, _ := ParseVer("1.0.0-beta.2")
	b, _ := ParseVer("1.0.0")
	if Cmp(a, b) != -1 {
		t.Fatal("prerelease must sort before release")
	}
}

func TestRenderEnvFillsSecrets(t *testing.T) {
	ex := "ATLAS_VERSION=0.2.0\nDB_PASSWORD=CHANGE_ME\nMANAGER_DATABASE_URL=postgresql://atlas:CHANGE_ME@db:5432/atlas\nATLAS_GATEWAY_AL_PLAN_KEY=CHANGE_ME\nOPENAI_API_KEYS=CHANGE_ME\nX=CHANGE_ME\n# c=CHANGE_ME"
	out := RenderEnv(ex, map[string]string{"ATLAS_VERSION": "0.3.0", "ATLAS_REGISTRY": "reg.local"})
	env := map[string]string{}
	for _, l := range strings.Split(out, "\n") {
		if k, v, ok := strings.Cut(l, "="); ok && !strings.HasPrefix(l, "#") {
			env[k] = v
		}
	}
	if env["ATLAS_VERSION"] != "0.3.0" || env["ATLAS_REGISTRY"] != "reg.local" || env["OPENAI_API_KEYS"] != "" ||
		!regexp.MustCompile(`^sk-atlas-prod-[0-9A-Za-z]{22}$`).MatchString(env["ATLAS_GATEWAY_AL_PLAN_KEY"]) ||
		!strings.Contains(env["MANAGER_DATABASE_URL"], env["DB_PASSWORD"]) || len(env["X"]) != 32 {
		t.Fatalf("env=%v", env)
	}
	if !strings.Contains(out, "# c=CHANGE_ME") {
		t.Fatal("comments must be preserved")
	}
}

func TestPlanUpgrade(t *testing.T) {
	compat := map[string]any{"versions": map[string]any{"0.3.0": map[string]any{"agentCore": ">=0.2.0 <1.0.0", "upgradeFrom": ">=0.2.0",
		"migrations": map[string]any{"manager": 4.0, "runtime": "0007"}}}}
	if _, err := PlanUpgrade(compat, "0.2.0", "0.3.0", map[string]string{"agentCore": "0.2.0"}); err != nil {
		t.Fatal(err)
	}
	if _, err := PlanUpgrade(compat, "0.2.0", "0.3.0", map[string]string{"agentCore": "0.1.0"}); err == nil || !strings.Contains(err.Error(), "지원 범위") {
		t.Fatalf("old agent core should block: %v", err)
	}
	if _, err := PlanUpgrade(compat, "0.4.0", "0.3.0", nil); err == nil || !strings.Contains(err.Error(), "다운그레이드") {
		t.Fatalf("downgrade should be refused: %v", err)
	}
	if _, err := PlanUpgrade(compat, "0.1.0", "0.3.0", nil); err == nil {
		t.Fatal("upgradeFrom should block 0.1.0")
	}
	if _, err := PlanUpgrade(compat, "0.2.0", "9.9.9", nil); err == nil {
		t.Fatal("unknown target")
	}
}

func TestInstallWritesBundleWithoutApply(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "atlas")
	v := Values{Dir: dir}
	v.Defaults()
	var log strings.Builder
	if err := Install(t.Context(), v, &log); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"compose.yaml", "install.sh", ".env", "atlas-values.yaml", "otel/collector.yaml", "compat.json"} {
		if _, err := os.Stat(filepath.Join(dir, f)); err != nil {
			t.Fatalf("missing %s: %v\n%s", f, err, log.String())
		}
	}
	env, _ := ReadEnv(filepath.Join(dir, ".env"))
	if strings.Contains(strings.Join(mapValues(env), ","), "CHANGE_ME") {
		t.Fatal("CHANGE_ME left in .env")
	}
	st, _ := os.Stat(filepath.Join(dir, ".env"))
	if st.Mode().Perm() != 0o600 {
		t.Fatalf(".env perm %v", st.Mode().Perm())
	}
	// 재실행은 기존 .env 를 보존
	before, _ := os.ReadFile(filepath.Join(dir, ".env"))
	_ = Install(t.Context(), v, &log)
	after, _ := os.ReadFile(filepath.Join(dir, ".env"))
	if string(before) != string(after) {
		t.Fatal(".env must be preserved on re-install")
	}
}

func mapValues(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for _, v := range m {
		out = append(out, v)
	}
	return out
}

func TestRenderEnvIAMSecrets(t *testing.T) {
	out := RenderEnv("ATLAS_MANAGER_BOOTSTRAP_KEY=CHANGE_ME\nATLAS_A2A_SIGNING_KEY=CHANGE_ME\nOPENFGA_PRESHARED_KEY=CHANGE_ME\n", nil)
	env := map[string]string{}
	for _, l := range strings.Split(out, "\n") {
		if k, v, ok := strings.Cut(l, "="); ok {
			env[k] = v
		}
	}
	if p := strings.SplitN(env["ATLAS_MANAGER_BOOTSTRAP_KEY"], "_", 4); len(p) != 4 || p[0] != "atl" || p[1] != "prod" || len(p[2]) != 26 {
		t.Fatalf("bootstrap key format: %s", env["ATLAS_MANAGER_BOOTSTRAP_KEY"])
	}
	pemBytes, err := base64.StdEncoding.DecodeString(env["ATLAS_A2A_SIGNING_KEY"])
	if err != nil || !strings.HasPrefix(string(pemBytes), "-----BEGIN EC PRIVATE KEY-----") {
		t.Fatalf("signing key: %v", err)
	}
	if len(env["OPENFGA_PRESHARED_KEY"]) != 32 || strings.Contains(out, "CHANGE_ME") {
		t.Fatalf("random secret: %q", out)
	}
}
