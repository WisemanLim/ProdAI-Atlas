package api

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"atlas-manager/internal/events"
	"atlas-manager/internal/iam"
	"atlas-manager/internal/store"
)

type authFixture struct {
	s   *Server
	h   http.Handler
	rec *events.Recorder
	key map[string]string // 이름 → 평문 키
}

func newAuthServer(t *testing.T) *authFixture {
	t.Helper()
	s, rec := newTestServer(t)
	s.AuthMode = AuthOIDC
	s.Audit = &iam.Audit{DB: s.DB, Engine: "sqlite"}
	s.Signer, _, _ = iam.NewSigner("", "atlas-manager")
	now := time.Now().UTC()
	for _, org := range []string{"acme", "beta"} {
		s.DB.Create(&store.Organization{ID: iam.ULID(), Slug: org, Name: org, Plan: "x", Status: "active", DataRegion: "kr", Isolation: "shared", CreatedAt: now, UpdatedAt: now})
		if err := createProjectTx(s.DB, org, "default", "Default", now); err != nil {
			t.Fatal(err)
		}
	}
	f := &authFixture{s: s, rec: rec, key: map[string]string{}}
	mk := func(name, org, scopes string) {
		plain, _, _, _ := iam.NewAPIKey("test")
		if _, err := BootstrapAPIKey(s.DB, plain, org, scopes); err != nil {
			t.Fatal(err)
		}
		f.key[name] = plain
	}
	mk("acme-dev", "acme", "read agents:write deployments:write a2a:invoke")
	mk("acme-ro", "acme", "read")
	mk("acme-admin", "acme", "admin")
	mk("beta-dev", "beta", "read agents:write")
	f.h = s.Router()
	return f
}

func (f *authFixture) do(method, path, key, body string, hdr ...string) (*httptest.ResponseRecorder, map[string]any) {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	for i := 0; i+1 < len(hdr); i += 2 {
		req.Header.Set(hdr[i], hdr[i+1])
	}
	if body != "" && strings.HasPrefix(body, "{") {
		req.Header.Set("Content-Type", "application/json")
	}
	w := httptest.NewRecorder()
	f.h.ServeHTTP(w, req)
	var out map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	return w, out
}

func manifestNamed(name string) string {
	return strings.Replace(manifestJSON, `"name":"al-plan"`, `"name":"`+name+`"`, 1)
}

func TestAuthRequiredAndPublicPaths(t *testing.T) {
	f := newAuthServer(t)
	if w, _ := f.do("GET", "/api/v1/agents", "", ""); w.Code != 401 || w.Header().Get("WWW-Authenticate") == "" {
		t.Fatalf("anonymous list = %d", w.Code)
	}
	for _, p := range []string{"/api/v1/catalog/units", "/api/v1/openapi.json", "/api/v1/auth/config", "/.well-known/jwks.json", "/health"} {
		if w, _ := f.do("GET", p, "", ""); w.Code != 200 {
			t.Fatalf("public %s = %d", p, w.Code)
		}
	}
	if w, _ := f.do("GET", "/api/v1/agents", "atl_test_00000000000000000000000000_nope", ""); w.Code != 401 {
		t.Fatalf("bad key = %d", w.Code)
	}
	if w, me := f.do("GET", "/api/v1/auth/me", f.key["acme-ro"], ""); w.Code != 200 || me["org"] != "acme" {
		t.Fatalf("me = %d %v", w.Code, me)
	}
}

func TestCrossTenantIsolation(t *testing.T) {
	f := newAuthServer(t)
	w, a := f.do("POST", "/api/v1/agents", f.key["acme-dev"], manifestJSON)
	if w.Code != 201 || a["org"] != "acme" {
		t.Fatalf("acme create = %d %s", w.Code, w.Body)
	}
	id := a["id"].(string)
	if w, _ := f.do("GET", "/api/v1/agents/"+id, f.key["beta-dev"], ""); w.Code != 404 {
		t.Fatalf("beta reads acme agent = %d (CMP-01 누출)", w.Code)
	}
	if _, l := f.do("GET", "/api/v1/agents", f.key["beta-dev"], ""); len(l["items"].([]any)) != 0 {
		t.Fatalf("beta list leaks: %v", l)
	}
	if w, _ := f.do("GET", "/api/v1/agents?org=acme", f.key["beta-dev"], ""); w.Code != 403 {
		t.Fatalf("beta key with ?org=acme = %d", w.Code)
	}
	// 같은 이름은 조직마다 따로 (유니크 인덱스에 org 포함)
	if w, _ := f.do("POST", "/api/v1/agents", f.key["beta-dev"], manifestJSON); w.Code != 201 {
		t.Fatalf("same name other org = %d", w.Code)
	}
}

func TestScopesAndDeniedAudit(t *testing.T) {
	f := newAuthServer(t)
	if w, _ := f.do("POST", "/api/v1/agents", f.key["acme-ro"], manifestJSON); w.Code != 403 {
		t.Fatalf("read-only create = %d", w.Code)
	}
	_, l := f.do("GET", "/api/v1/orgs/acme/audit?action=authz.denied", f.key["acme-admin"], "")
	if items := l["items"].([]any); len(items) != 1 || items[0].(map[string]any)["result"] != "denied" {
		t.Fatalf("denied audit: %v", l)
	}
	if w, _ := f.do("GET", "/api/v1/orgs/acme/audit", f.key["acme-ro"], ""); w.Code != 403 {
		t.Fatalf("read-only audit = %d", w.Code)
	}
}

func TestQuotaAgentsMax(t *testing.T) {
	f := newAuthServer(t)
	f.s.DB.Create(&store.Quota{OrgID: "acme", Scope: "org", ScopeID: "acme", Qkey: "agents.max", Qlimit: 1})
	if w, _ := f.do("POST", "/api/v1/agents", f.key["acme-dev"], manifestNamed("one")); w.Code != 201 {
		t.Fatalf("first = %d", w.Code)
	}
	w, out := f.do("POST", "/api/v1/agents", f.key["acme-dev"], manifestNamed("two"))
	if w.Code != 429 || out["error"] != "quota_exceeded" {
		t.Fatalf("quota = %d %v", w.Code, out)
	}
	if !strings.Contains(strings.Join(f.rec.Types(), ","), "plt.quota.exceeded") {
		t.Fatalf("event missing: %v", f.rec.Types())
	}
	// 프로젝트 쿼터 합은 조직 한도를 넘을 수 없다
	if w, _ := f.do("PUT", "/api/v1/orgs/acme/quotas", f.key["acme-admin"], `{"scope":"project","project":"default","key":"agents.max","limit":5}`); w.Code != 422 {
		t.Fatalf("child > parent = %d", w.Code)
	}
	if w, _ := f.do("GET", "/api/v1/orgs/acme/quotas", f.key["acme-ro"], ""); w.Code != 200 || !strings.Contains(w.Body.String(), `"used":1`) {
		t.Fatalf("quota list: %s", w.Body)
	}
}

func readyAgent(t *testing.T, f *authFixture, endpoint string) string {
	t.Helper()
	_, a := f.do("POST", "/api/v1/agents", f.key["acme-dev"], manifestJSON)
	id := a["id"].(string)
	now := time.Now().UTC()
	f.s.DB.Create(&store.Deployment{ID: iam.ULID(), OrgID: "acme", AgentID: id, Rev: 1, Env: "dev", Driver: "compose", Endpoint: endpoint,
		Status: store.DeployReady, CreatedAt: now, UpdatedAt: now})
	return id
}

func TestA2AProxyTenantJWTAndMismatch(t *testing.T) {
	f := newAuthServer(t)
	var gotAuth, gotBody, gotBaggage string
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth, gotBaggage = r.Header.Get("Authorization"), r.Header.Get("baggage")
		b, _ := io.ReadAll(r.Body)
		gotBody = string(b)
		_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{}}`))
	}))
	defer agent.Close()
	id := readyAgent(t, f, agent.URL)
	w, _ := f.do("POST", "/api/v1/agents/"+id+"/a2a", f.key["acme-dev"], `{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"parts":[{"text":"x"}]}}}`)
	if w.Code != 200 || !strings.Contains(gotBody, `"tenant":"acme"`) || !strings.Contains(gotBaggage, "atlas.tenant=acme") {
		t.Fatalf("proxy: %d body=%s baggage=%s", w.Code, gotBody, gotBaggage)
	}
	c, err := f.s.Signer.Parse(strings.TrimPrefix(gotAuth, "Bearer "))
	if err != nil || c.Org != "acme" || c.Audience[0] != "agent:"+id {
		t.Fatalf("tenant jwt: %+v %v", c, err)
	}
	w, out := f.do("POST", "/api/v1/agents/"+id+"/a2a", f.key["acme-dev"], `{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"tenant":"beta","message":{"parts":[{"text":"x"}]}}}`)
	if w.Code != 403 || out["error"] != "tenant_mismatch" {
		t.Fatalf("mismatch = %d %v", w.Code, out)
	}
	_, l := f.do("GET", "/api/v1/orgs/acme/audit?action=security.tenant_mismatch", f.key["acme-admin"], "")
	if len(l["items"].([]any)) != 1 {
		t.Fatalf("tenant_mismatch audit: %v", l)
	}
	// 조직 정지 → 에이전트 호출 401 (FR-PLT-TEN-01)
	f.s.DB.Model(&store.Organization{}).Where("slug = 'acme'").Update("status", "suspended")
	if w, out := f.do("POST", "/api/v1/agents/"+id+"/a2a", f.key["acme-dev"], `{}`); w.Code != 401 || out["error"] != "org_suspended" {
		t.Fatalf("suspended = %d %v", w.Code, out)
	}
}

func TestAPIKeyLifecycleAndAuditChain(t *testing.T) {
	f := newAuthServer(t)
	w, out := f.do("POST", "/api/v1/orgs/acme/apikeys", f.key["acme-admin"], `{"name":"ci","scopes":"read","expiresInDays":30}`)
	if w.Code != 201 {
		t.Fatalf("create key = %d %s", w.Code, w.Body)
	}
	plain := out["key"].(string)
	if w, _ := f.do("POST", "/api/v1/orgs/acme/apikeys", f.key["acme-admin"], `{"name":"x","scopes":"read","expiresInDays":400}`); w.Code != 400 {
		t.Fatalf("expiry > 365 accepted: %d", w.Code)
	}
	if w, _ := f.do("POST", "/api/v1/orgs/acme/apikeys", f.key["acme-admin"], `{"name":"x","scopes":"admin","expiresInDays":1}`); w.Code != 403 {
		t.Fatalf("admin-scope key by non-platform = %d", w.Code)
	}
	if w, _ := f.do("GET", "/api/v1/agents", plain, ""); w.Code != 200 {
		t.Fatalf("new key = %d", w.Code)
	}
	if strings.Contains(f.do2("GET", "/api/v1/orgs/acme/apikeys", f.key["acme-admin"]), plain[len(plain)-10:]) {
		t.Fatal("list leaks secret")
	}
	kid := out["apiKey"].(map[string]any)["id"].(string)
	if w, _ := f.do("DELETE", "/api/v1/orgs/acme/apikeys/"+kid, f.key["acme-admin"], ""); w.Code != 204 {
		t.Fatalf("revoke = %d", w.Code)
	}
	if w, _ := f.do("GET", "/api/v1/agents", plain, ""); w.Code != 401 {
		t.Fatalf("revoked key = %d", w.Code)
	}
	_, v := f.do("GET", "/api/v1/orgs/acme/audit/verify", f.key["acme-admin"], "")
	if v["valid"] != true || v["events"].(float64) < 2 {
		t.Fatalf("audit verify: %v", v)
	}
	if lines := strings.Count(f.do2("GET", "/api/v1/orgs/acme/audit/export", f.key["acme-admin"]), "\n"); lines < 2 {
		t.Fatalf("export lines: %d", lines)
	}
}

func (f *authFixture) do2(method, path, key string) string {
	w, _ := f.do(method, path, key, "")
	return w.Body.String()
}

func TestSvcProxyTenantEnforcement(t *testing.T) {
	f := newAuthServer(t)
	var seen *http.Request
	svc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { seen = r; _, _ = w.Write([]byte(`{}`)) }))
	defer svc.Close()
	u, _ := url.Parse(svc.URL)
	f.s.Services = map[string]*url.URL{"gr": u}
	f.h = f.s.Router()
	front := httptest.NewServer(f.h) // ReverseProxy 는 CloseNotifier 가 필요 → 실제 서버
	defer front.Close()
	call := func(method, path, key, b string) int {
		req, _ := http.NewRequest(method, front.URL+path, strings.NewReader(b))
		req.Header.Set("Authorization", "Bearer "+key)
		if b != "" {
			req.Header.Set("Content-Type", "application/json")
		}
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		return res.StatusCode
	}
	if code := call("GET", "/svc/gr/v1/guard/verdicts?tenant=acme", f.key["acme-ro"], ""); code != 200 || seen.Header.Get("Authorization") != "" || seen.Header.Get("X-Atlas-Org") != "acme" {
		t.Fatalf("svc get = %d auth=%q org=%q", code, seen.Header.Get("Authorization"), seen.Header.Get("X-Atlas-Org"))
	}
	if code := call("GET", "/svc/gr/v1/guard/verdicts?tenant=beta", f.key["acme-ro"], ""); code != 403 {
		t.Fatalf("query tenant mismatch = %d", code)
	}
	if code := call("POST", "/svc/gr/v1/guard/check", f.key["acme-ro"], `{"tenant":"acme"}`); code != 403 {
		t.Fatalf("read-only svc mutation = %d", code)
	}
	if code := call("POST", "/svc/gr/v1/guard/check", f.key["acme-admin"], `{"tenant":"beta","contents":["x"]}`); code != 403 {
		t.Fatalf("body tenant mismatch = %d", code)
	}
	if code := call("POST", "/svc/gr/v1/guard/check", f.key["acme-admin"], `{"tenant":"acme","contents":["x"]}`); code != 200 {
		t.Fatalf("own tenant = %d", code)
	}
	if code := call("GET", "/svc/gr/admin/x", f.key["acme-admin"], ""); code != 403 {
		t.Fatalf("service admin path = %d", code)
	}
}
