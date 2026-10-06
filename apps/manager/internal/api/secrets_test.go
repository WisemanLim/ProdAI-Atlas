package api

import (
	"context"
	"strings"
	"testing"

	"atlas-manager/internal/iam"
	"atlas-manager/internal/secrets"
)

func withSecrets(t *testing.T) *authFixture {
	f := newAuthServer(t)
	st, err := secrets.Open("file://" + t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	f.s.Secrets = st
	f.h = f.s.Router()
	return f
}

const secretManifest = `{"apiVersion":"agents.prodai.io/v1alpha1","kind":"AtlasAgent","metadata":{"name":"gr-probe","unitId":"GR-IO"},
 "spec":{"version":"0.1.0","interfaces":[{"binding":"JSONRPC","port":8000}],"secrets":[{"name":"guard-secret","required":true}]}}`

func TestSecretBrokerLeastPrivilegeAndAudit(t *testing.T) {
	f := withSecrets(t)
	if w, out := f.do("PUT", "/api/v1/orgs/acme/secrets/guard-secret", f.key["acme-admin"], `{"value":"hmac-v1"}`); w.Code != 200 || out["version"] != float64(1) {
		t.Fatalf("put = %d %v", w.Code, out)
	}
	f.do("PUT", "/api/v1/orgs/acme/secrets/other", f.key["acme-admin"], `{"value":"nope"}`)
	if w, _ := f.do("PUT", "/api/v1/orgs/acme/secrets/x", f.key["beta-dev"], `{"value":"v"}`); w.Code != 403 {
		t.Fatalf("cross-org write = %d", w.Code)
	}
	if w, _ := f.do("PUT", "/api/v1/orgs/acme/secrets/x", f.key["acme-dev"], `{"value":"v"}`); w.Code != 403 {
		t.Fatalf("non-admin write = %d", w.Code)
	}
	w, _ := f.do("GET", "/api/v1/orgs/acme/secrets", f.key["acme-admin"], "")
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"guard-secret"`) || strings.Contains(w.Body.String(), "hmac-v1") {
		t.Fatalf("list leaks or misses: %s", w.Body)
	}
	if w, out := f.do("GET", "/api/v1/secrets/guard-secret", f.key["acme-admin"], ""); w.Code != 403 || out["error"] != "agent_only" {
		t.Fatalf("admin read value = %d %v", w.Code, out)
	}
	_, a := f.do("POST", "/api/v1/agents", f.key["acme-dev"], secretManifest)
	id := a["id"].(string)
	if w, _ := f.do("POST", "/api/v1/agents/"+id+"/token", f.key["acme-ro"], ""); w.Code != 403 {
		t.Fatalf("read-only key mints agent token = %d", w.Code)
	}
	_, tok := f.do("POST", "/api/v1/agents/"+id+"/token", f.key["acme-dev"], "")
	at := tok["token"].(string)
	if w, out := f.do("GET", "/api/v1/secrets/guard-secret", at, ""); w.Code != 200 || out["value"] != "hmac-v1" || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("declared read = %d %v", w.Code, out)
	}
	if w, out := f.do("GET", "/api/v1/secrets/other", at, ""); w.Code != 403 || out["error"] != "secret_not_declared" {
		t.Fatalf("undeclared read = %d %v", w.Code, out)
	}
	if w, out := f.do("POST", "/api/v1/orgs/acme/secrets/guard-secret/rotate", f.key["acme-admin"], `{"value":"hmac-v2"}`); w.Code != 200 || out["previousVersion"] != float64(1) {
		t.Fatalf("rotate = %d %v", w.Code, out)
	}
	if _, out := f.do("GET", "/api/v1/secrets/guard-secret", at, ""); out["value"] != "hmac-v2" || out["version"] != float64(2) {
		t.Fatalf("after rotate: %v", out)
	}
	// 에이전트 토큰은 같은 조직 조회·위임만 — 관리(쓰기) API 는 못 쓴다
	if w, _ := f.do("PUT", "/api/v1/orgs/acme/secrets/x", at, `{"value":"v"}`); w.Code != 403 {
		t.Fatalf("agent token on admin API = %d", w.Code)
	}
	w, _ = f.do("GET", "/api/v1/orgs/acme/audit?limit=50", f.key["acme-admin"], "")
	body := w.Body.String()
	for _, want := range []string{`"action":"secret.write"`, `"action":"secret.rotate"`, `"action":"secret.read","resource":"secret:guard-secret"`, `"result":"denied"`} {
		if !strings.Contains(body, want) {
			t.Fatalf("audit missing %s", want)
		}
	}
	if strings.Contains(body, "hmac-v") {
		t.Fatal("audit leaks secret value")
	}
}

func TestImportSecretsFromEnv(t *testing.T) {
	st, _ := secrets.Open("file://" + t.TempDir())
	env := map[string]string{"G": "g-val", "C": "CHANGE_ME"}
	got, err := ImportSecrets(context.Background(), st, "default/guard-secret=G,default/ch=C,bad", func(k string) string { return env[k] })
	if err != nil || len(got) != 1 || got[0] != "default/guard-secret" {
		t.Fatalf("import: %v %v", got, err)
	}
	env["G"] = "changed"
	again, _ := ImportSecrets(context.Background(), st, "default/guard-secret=G", func(k string) string { return env[k] })
	s, _ := st.Get(context.Background(), "orgs/default/guard-secret")
	if len(again) != 0 || s.Value != "g-val" {
		t.Fatal("store must stay source of truth once imported")
	}
}

func TestServiceKeyWritesOnlySvcPrefixedSecrets(t *testing.T) {
	f := withSecrets(t)
	plain, _, _, _ := iam.NewAPIKey("test")
	if _, err := BootstrapAPIKey(f.s.DB, plain, "acme", "read secrets:write"); err != nil {
		t.Fatal(err)
	}
	if w, _ := f.do("PUT", "/api/v1/orgs/acme/secrets/svc-mkt-1-api-key", plain, `{"value":"v"}`); w.Code != 200 {
		t.Fatalf("svc- write = %d", w.Code)
	}
	for _, c := range []struct{ method, path string }{{"PUT", "/api/v1/orgs/acme/secrets/guard-secret"}, {"PUT", "/api/v1/orgs/beta/secrets/svc-x"},
		{"POST", "/api/v1/orgs/acme/secrets/svc-mkt-1-api-key/rotate"}} {
		if w, _ := f.do(c.method, c.path, plain, `{"value":"v"}`); w.Code != 403 && w.Code != 404 {
			t.Fatalf("%s %s = %d", c.method, c.path, w.Code)
		}
	}
}

func TestGatewayAudienceTokenCannotCallManager(t *testing.T) {
	f := withSecrets(t)
	_, a := f.do("POST", "/api/v1/agents", f.key["acme-dev"], secretManifest)
	id := a["id"].(string)
	_, gw := f.do("POST", "/api/v1/agents/"+id+"/token?audience=atlas-mcp-gateway", f.key["acme-dev"], "")
	if gw["audience"] != "atlas-mcp-gateway" {
		t.Fatalf("token = %v", gw)
	}
	if w, _ := f.do("GET", "/api/v1/agents", gw["token"].(string), ""); w.Code != 401 {
		t.Fatalf("gateway token on manager = %d", w.Code)
	}
	if w, _ := f.do("POST", "/api/v1/agents/"+id+"/token?audience=evil", f.key["acme-dev"], ""); w.Code != 400 {
		t.Fatalf("bad audience = %d", w.Code)
	}
}
