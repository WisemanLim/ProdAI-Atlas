package api

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"atlas-manager/internal/iam"
	"atlas-manager/internal/store"
)

func TestSCIMProvisioningAndDeprovision(t *testing.T) {
	f := newAuthServer(t)
	scim, _ := extraKey(t, f, "acme", "scim")
	betaScim, _ := extraKey(t, f, "beta", "scim")
	now := time.Now().UTC()
	f.s.DB.Create(&store.APIKey{ID: iam.ULID(), OrgID: "acme", Prefix: "atl_test_alicekey", Hash: "x", Salt: "x", Name: "alice-ci", Owner: "alice",
		Scopes: "read", ExpiresAt: now.Add(time.Hour), CreatedAt: now})
	if w, _ := f.do("GET", "/scim/v2/Users", f.key["acme-dev"], ""); w.Code != 401 {
		t.Fatalf("non-scim key = %d", w.Code)
	}
	w, u := f.do("POST", "/scim/v2/Users", scim, `{"schemas":["urn:ietf:params:scim:schemas:core:2.0:User"],"userName":"alice","externalId":"kc-alice","emails":[{"value":"alice@example.test","primary":true}]}`)
	if w.Code != 201 || w.Header().Get("Content-Type") != "application/scim+json" || u["active"] != true {
		t.Fatalf("create = %d %v", w.Code, u)
	}
	uid := u["id"].(string)
	if w, e := f.do("POST", "/scim/v2/Users", scim, `{"userName":"alice"}`); w.Code != 409 || e["scimType"] != "uniqueness" {
		t.Fatalf("dup = %d %v", w.Code, e)
	}
	if _, l := f.do("GET", "/scim/v2/Users?filter="+url.QueryEscape(`userName eq "alice"`), scim, ""); l["totalResults"] != float64(1) {
		t.Fatalf("filter = %v", l)
	}
	if w, _ := f.do("GET", "/scim/v2/Users/"+uid, betaScim, ""); w.Code != 404 {
		t.Fatalf("cross-org scim = %d", w.Code)
	}
	w, g := f.do("POST", "/scim/v2/Groups", scim, `{"displayName":"eng","members":[{"value":"`+uid+`"}]}`)
	if w.Code != 201 || len(g["members"].([]any)) != 1 {
		t.Fatalf("group = %d %v", w.Code, g)
	}
	gid := g["id"].(string)
	if w, _ := f.do("PUT", "/api/v1/orgs/acme/scim/group-roles", f.key["acme-admin"], `{"group":"eng","role":"viewer"}`); w.Code != 200 {
		t.Fatalf("group-role = %d", w.Code)
	}
	_, gu := f.do("GET", "/scim/v2/Users/"+uid, scim, "")
	if gs := gu["groups"].([]any); len(gs) != 1 || gs[0].(map[string]any)["display"] != "eng" {
		t.Fatalf("user groups = %v", gu["groups"])
	}
	w, pg := f.do("PATCH", "/scim/v2/Groups/"+gid, scim, `{"schemas":["urn:ietf:params:scim:api:messages:2.0:PatchOp"],"Operations":[{"op":"remove","path":"members[value eq \"`+uid+`\"]"}]}`)
	if w.Code != 200 || len(pg["members"].([]any)) != 0 {
		t.Fatalf("patch group = %d %v", w.Code, pg)
	}
	// Entra 형식: path 없는 replace + "False" 문자열
	w, pu := f.do("PATCH", "/scim/v2/Users/"+uid, scim, `{"schemas":["urn:ietf:params:scim:api:messages:2.0:PatchOp"],"Operations":[{"op":"Replace","value":{"active":"False"}}]}`)
	if w.Code != 200 || pu["active"] != false {
		t.Fatalf("deactivate = %d %v", w.Code, pu)
	}
	var k store.APIKey
	f.s.DB.First(&k, "prefix = ?", "atl_test_alicekey")
	var rv store.SessionRevocation
	if k.RevokedAt == nil || f.s.DB.First(&rv, "sub = ?", "kc-alice").Error != nil {
		t.Fatalf("deprovision: key revoked=%v revocation=%v", k.RevokedAt, rv)
	}
	if !strings.Contains(strings.Join(f.rec.Types(), ","), "plt.iam.user_deprovisioned") {
		t.Fatalf("event: %v", f.rec.Types())
	}
	// 비활성 사용자 토큰은 세션 검사에서 거부
	c := testCtx("acme", iam.Principal{Kind: "user", Sub: "kc-alice", Name: "alice", IssuedAt: now.Add(time.Hour)})
	if code, _ := f.s.checkSession(c, principal(c)); code != "user_deactivated" {
		t.Fatalf("deactivated session = %q", code)
	}
	if w, _ := f.do("DELETE", "/scim/v2/Users/"+uid, scim, ""); w.Code != 204 {
		t.Fatalf("delete = %d", w.Code)
	}
}

func testCtx(org string, p iam.Principal) *gin.Context {
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest("GET", "/api/v1/agents?org="+org, nil)
	c.Set("principal", p)
	return c
}

func TestSessionPolicyAndMFA(t *testing.T) {
	f := newAuthServer(t)
	now := time.Now().UTC()
	f.s.DB.Create(&store.OrgSecurity{OrgID: "acme", MfaAdmin: true, AcrMin: 2, IdleS: 60, MaxS: 3600, MaxSessions: 1, UpdatedAt: now})
	u := iam.Principal{Kind: "user", Sub: "u1", Sid: "s1", Acr: 1, AuthTime: now, IssuedAt: now}
	if code, _ := f.s.checkSession(testCtx("acme", u), u); code != "" {
		t.Fatalf("fresh = %q", code)
	}
	old := u
	old.AuthTime = now.Add(-2 * time.Hour)
	if code, _ := f.s.checkSession(testCtx("acme", old), old); code != "session_expired" {
		t.Fatalf("max = %q", code)
	}
	u2 := u
	u2.Sid = "s2"
	if code, _ := f.s.checkSession(testCtx("acme", u2), u2); code != "" {
		t.Fatalf("second = %q", code)
	}
	if code, _ := f.s.checkSession(testCtx("acme", u), u); code != "session_revoked" { // 동시 세션 1 → 오래된 s1 폐기
		t.Fatalf("concurrent = %q", code)
	}
	f.s.DB.Model(&store.AuthSession{}).Where("sid = ?", "s2").Update("last_seen", now.Add(-2*time.Minute))
	if code, _ := f.s.checkSession(testCtx("acme", u2), u2); code != "session_idle" {
		t.Fatalf("idle = %q", code)
	}
	c := testCtx("acme", u)
	if f.s.requireMFA(c, "admin", orgObj("acme")) || c.Writer.Status() != 401 || !strings.Contains(c.Writer.Header().Get("WWW-Authenticate"), "insufficient_user_authentication") {
		t.Fatalf("mfa: status=%d hdr=%q", c.Writer.Status(), c.Writer.Header().Get("WWW-Authenticate"))
	}
	u.Acr = 2
	if !f.s.requireMFA(testCtx("acme", u), "admin", orgObj("acme")) || !f.s.requireMFA(testCtx("acme", iam.Principal{Kind: "user", Acr: 0}), "viewer", orgObj("acme")) {
		t.Fatal("acr 2 · viewer 는 통과")
	}
}

func TestWorkloadIdentityAndTokenExchange(t *testing.T) {
	f := newAuthServer(t)
	_, a := f.do("POST", "/api/v1/agents", f.key["acme-dev"], manifestNamed("caller"))
	_, b := f.do("POST", "/api/v1/agents", f.key["acme-dev"], manifestNamed("callee"))
	_, x := f.do("POST", "/api/v1/agents", f.key["beta-dev"], manifestNamed("other"))
	aid := a["id"].(string)
	w, tk := f.do("POST", "/api/v1/agents/"+aid+"/token?audience=agent:"+b["id"].(string)+"&env=prod", f.key["acme-dev"], "")
	if w.Code != 200 || tk["spiffeId"] != "spiffe://atlas/acme/default/prod/caller" {
		t.Fatalf("peer token = %d %v", w.Code, tk)
	}
	cl, _ := f.s.Signer.Parse(tk["token"].(string))
	if cl.SpiffeID != tk["spiffeId"] || cl.Audience[0] != "agent:"+b["id"].(string) {
		t.Fatalf("claims = %+v", cl)
	}
	if w, _ := f.do("POST", "/api/v1/agents/"+aid+"/token?audience=agent:"+x["id"].(string), f.key["acme-dev"], ""); w.Code != 400 {
		t.Fatalf("cross-org peer = %d", w.Code)
	}
	_, mt := f.do("POST", "/api/v1/agents/"+aid+"/token", f.key["acme-dev"], "")
	actor := mt["token"].(string)
	form := func(scope string) string {
		return url.Values{"grant_type": {tokenExchangeGrant}, "subject_token": {f.key["acme-ro"]}, "scope": {scope}}.Encode()
	}
	ct := []string{"Content-Type", "application/x-www-form-urlencoded"}
	if w, e := f.do("POST", "/api/v1/token/exchange", actor, form("agents:write"), ct...); w.Code != 400 || e["error"] != "invalid_scope" {
		t.Fatalf("widen = %d %v", w.Code, e)
	}
	if w, _ := f.do("POST", "/api/v1/token/exchange", f.key["acme-ro"], form("read"), ct...); w.Code != 401 {
		t.Fatalf("non-agent actor = %d", w.Code)
	}
	w, ex := f.do("POST", "/api/v1/token/exchange", actor, form("read"), ct...)
	if w.Code != 200 || ex["scope"] != "read" || ex["issued_token_type"] != "urn:ietf:params:oauth:token-type:jwt" {
		t.Fatalf("exchange = %d %v", w.Code, ex)
	}
	del := ex["access_token"].(string)
	dc, _ := f.s.Signer.Parse(del)
	if dc.Act == nil || dc.Act.Sub != "agent:"+aid || !strings.HasPrefix(dc.Subject, "apikey:") {
		t.Fatalf("delegated claims = %+v", dc)
	}
	if w, _ := f.do("GET", "/api/v1/agents", del, ""); w.Code != 200 {
		t.Fatalf("delegated read = %d", w.Code)
	}
	if w, _ := f.do("POST", "/api/v1/agents", del, manifestNamed("nope")); w.Code != 403 {
		t.Fatalf("delegated write = %d", w.Code)
	}
}

func TestBudgetInheritancePushesToGateway(t *testing.T) {
	f := newAuthServer(t)
	var mu sync.Mutex
	got := map[string]map[string]any{}
	gw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if r.Method == http.MethodPut {
			var b map[string]any
			raw, _ := io.ReadAll(r.Body)
			_ = json.Unmarshal(raw, &b)
			got[b["id"].(string)] = b
		}
		if r.Method == http.MethodGet {
			_, _ = w.Write([]byte(`{"data":[{"id":"mgr.acme.org-m","spent_usd":"12.5","pct":12,"state":"ok"}]}`))
		}
	}))
	defer gw.Close()
	u, _ := url.Parse(gw.URL)
	f.s.Services = map[string]*url.URL{"gw": u}
	put := func(id, body string) (int, map[string]any) {
		w, out := f.do("PUT", "/api/v1/orgs/acme/budgets/"+id, f.key["acme-admin"], body)
		return w.Code, out
	}
	if code, _ := put("org-m", `{"scope":"org","amountUsd":"100"}`); code != 200 {
		t.Fatalf("org = %d", code)
	}
	if code, _ := put("p-default", `{"scope":"project","project":"default","amountUsd":"60","hardAction":"downgrade","downgradeAlias":"cheap"}`); code != 200 {
		t.Fatalf("project = %d", code)
	}
	if code, e := put("p-ml", `{"scope":"project","project":"ml","amountUsd":"50"}`); code != 422 || e["error"] != "budget_exceeds_parent" {
		t.Fatalf("over parent = %d %v", code, e)
	}
	if code, e := put("e-prod", `{"scope":"env","project":"default","env":"prod","amountUsd":"70"}`); code != 422 || e["error"] != "budget_exceeds_parent" {
		t.Fatalf("env over project = %d %v", code, e)
	}
	if code, _ := put("e-prod", `{"scope":"env","project":"default","env":"prod","amountUsd":"40","hardAction":"notify"}`); code != 200 {
		t.Fatalf("env = %d", code)
	}
	if code, e := put("org-m", `{"scope":"org","amountUsd":"50"}`); code != 422 || e["error"] != "budget_below_children" {
		t.Fatalf("parent below children = %d %v", code, e)
	}
	if w, _ := f.do("PUT", "/api/v1/orgs/acme/budgets/x", f.key["acme-ro"], `{"scope":"org","amountUsd":"1"}`); w.Code != 403 {
		t.Fatalf("read key budget = %d", w.Code)
	}
	mu.Lock()
	env, prj := got["mgr.acme.e-prod"], got["mgr.acme.p-default"]
	mu.Unlock()
	if env["scope"] != "env" || env["scope_id"] != "acme/default/prod" || env["hard_action"] != "alert" || prj["scope_id"] != "acme/default" || prj["downgrade_alias"] != "cheap" {
		t.Fatalf("gateway payloads: env=%v project=%v", env, prj)
	}
	_, l := f.do("GET", "/api/v1/orgs/acme/budgets", f.key["acme-ro"], "")
	if !strings.Contains(mustJSON(l), `"spentUsd":"12.5"`) {
		t.Fatalf("list = %v", l)
	}
}

func TestPlanQuotasAndThresholds(t *testing.T) {
	s, rec := newTestServer(t) // auth off = 플랫폼 관리자
	h := s.Router()
	if w, _ := do(t, h, "PUT", "/api/v1/plans/team", `{"name":"Team","limits":{"max_agents":1,"max_concurrency":1,"monthly_tokens":100}}`); w.Code != 200 {
		t.Fatalf("plan = %d", w.Code)
	}
	s.DB.Model(&store.Organization{}).Where("slug = ?", "default").Update("plan", "team") // 시드 조직
	if w, _ := do(t, h, "POST", "/api/v1/agents", manifestNamed("one")); w.Code != 201 {
		t.Fatalf("first agent = %d", w.Code)
	}
	if w, e := do(t, h, "POST", "/api/v1/agents", manifestNamed("two")); w.Code != 429 || e["error"] != "quota_exceeded" {
		t.Fatalf("plan max_agents = %d %v", w.Code, e)
	}
	ctx := testCtx("default", iam.Principal{Kind: "anonymous", PlatformAdmin: true})
	release, ok := s.admitRun(ctx, "default")
	if !ok {
		t.Fatal("first run admitted")
	}
	c2 := testCtx("default", iam.Principal{Kind: "anonymous", PlatformAdmin: true})
	if _, ok := s.admitRun(c2, "default"); ok || c2.Writer.Status() != 429 {
		t.Fatalf("concurrency = %d", c2.Writer.Status())
	}
	release()
	_ = s.addUsage(ctx, "default", 85)
	_ = s.addUsage(ctx, "default", 5)
	_ = s.addUsage(ctx, "default", 20)
	c3 := testCtx("default", iam.Principal{Kind: "anonymous", PlatformAdmin: true})
	if _, ok := s.admitRun(c3, "default"); ok || c3.Writer.Status() != 429 {
		t.Fatalf("monthly tokens = %d", c3.Writer.Status())
	}
	levels := []float64{}
	for _, e := range rec.Events {
		if e.Type == "plt.quota.threshold" {
			if d, _ := e.Data.(gin.H); d["key"] == "tokens.monthly" {
				levels = append(levels, float64(d["level"].(int)))
			}
		}
	}
	if len(levels) != 2 || levels[0] != 80 || levels[1] != 100 {
		t.Fatalf("thresholds once each: %v", levels)
	}
	w, us := do(t, h, "GET", "/api/v1/orgs/default/usage", "")
	if w.Code != 200 || us["plan"] != "team" || us["used"].(map[string]any)["monthlyTokens"] != float64(110) {
		t.Fatalf("usage = %d %v", w.Code, us)
	}
}
