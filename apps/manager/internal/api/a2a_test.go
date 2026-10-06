package api

import (
	"strings"
	"testing"
	"time"

	"atlas-manager/internal/iam"
	"atlas-manager/internal/store"
)

func deployWithCard(t *testing.T, f *authFixture, key, name, vis, skillID, desc string) string {
	t.Helper()
	m := strings.Replace(manifestJSON, `"name":"al-plan"`, `"name":"`+name+`"`, 1)
	m = strings.Replace(m, `"spec":{`, `"spec":{"agentCard":{"visibility":"`+vis+`"},`, 1)
	w, a := f.do("POST", "/api/v1/agents", key, m)
	if w.Code != 201 {
		t.Fatalf("create %s = %d %s", name, w.Code, w.Body)
	}
	card := `{"name":"` + name + `","description":"` + desc + `","skills":[{"id":"` + skillID + `","name":"` + skillID + `","description":"` + desc + `","tags":["x"],"inputModes":["application/json"]}]}`
	now := time.Now().UTC()
	f.s.DB.Create(&store.Deployment{ID: iam.ULID(), OrgID: a["org"].(string), AgentID: a["id"].(string), Rev: 1, Env: "dev", Driver: "compose",
		Endpoint: "http://x", Status: store.DeployReady, Card: &card, CreatedAt: now, UpdatedAt: now})
	return a["id"].(string)
}

func TestSignedCardJCSAndVerify(t *testing.T) {
	f := newAuthServer(t)
	id := deployWithCard(t, f, f.key["acme-dev"], "reviewer", "tenant", "ma.code_review", "코드 리뷰 diff 분석 <보안>")
	w, card := f.do("GET", "/api/v1/agents/"+id+"/card", f.key["acme-ro"], "")
	if w.Code != 200 {
		t.Fatalf("card = %d %s", w.Code, w.Body)
	}
	sigs := card["signatures"].([]any)[0].(map[string]any)
	delete(card, "signatures")
	payload, _ := Canonical(card)
	if strings.Contains(string(payload), "\\u003c") || !strings.Contains(string(payload), "<보안>") || !strings.HasPrefix(string(payload), `{"description":`) {
		t.Fatalf("JCS: keys sorted, no HTML escape: %s", payload[:60])
	}
	if err := f.s.Signer.VerifyJWS(sigs["protected"].(string), sigs["signature"].(string), payload); err != nil {
		t.Fatalf("signature: %v", err)
	}
	card["description"] = "tampered"
	p2, _ := Canonical(card)
	if f.s.Signer.VerifyJWS(sigs["protected"].(string), sigs["signature"].(string), p2) == nil {
		t.Fatal("tampered card verified")
	}
	if md := card["metadata"].(map[string]any); md["atlas.org"] != "acme" || md["atlas.visibility"] != "tenant" {
		t.Fatalf("metadata: %v", md)
	}
}

func TestRegistryBM25AndVisibility(t *testing.T) {
	f := newAuthServer(t)
	deployWithCard(t, f, f.key["acme-dev"], "reviewer", "tenant", "ma.code_review", "코드 리뷰 diff security bug")
	deployWithCard(t, f, f.key["acme-dev"], "researcher", "tenant", "ma.research", "문서 조사 research citations")
	pub := deployWithCard(t, f, f.key["beta-dev"], "public-analyst", "public", "ma.analysis", "데이터 분석 chart table")
	deployWithCard(t, f, f.key["beta-dev"], "beta-secret", "tenant", "ma.research", "research beta private")
	_, out := f.do("GET", "/api/v1/a2a/registry/agents?q=code+review+diff", f.key["acme-ro"], "")
	items := out["items"].([]any)
	if len(items) == 0 || items[0].(map[string]any)["name"] != "reviewer" {
		t.Fatalf("bm25 top: %v", items)
	}
	_, all := f.do("GET", "/api/v1/a2a/registry/agents", f.key["acme-ro"], "")
	names := map[string]bool{}
	for _, it := range all["items"].([]any) {
		names[it.(map[string]any)["name"].(string)] = true
	}
	if !names["public-analyst"] || names["beta-secret"] || !names["researcher"] {
		t.Fatalf("visibility: %v", names)
	}
	_, sk := f.do("GET", "/api/v1/a2a/registry/agents?skill=ma.research", f.key["acme-ro"], "")
	if n := len(sk["items"].([]any)); n != 1 {
		t.Fatalf("skill filter: %d", n)
	}
	if w, _ := f.do("GET", "/api/v1/agents/"+pub+"/card", f.key["acme-ro"], ""); w.Code != 200 {
		t.Fatalf("public card from other org = %d", w.Code)
	}
	priv := deployWithCard(t, f, f.key["acme-dev"], "priv", "private", "ma.x", "private")
	if w, _ := f.do("GET", "/api/v1/agents/"+priv+"/card", f.key["beta-dev"], ""); w.Code != 404 {
		t.Fatalf("other org private card = %d", w.Code)
	}
}

func TestAgentPrincipalCanInvokeSameOrgOnly(t *testing.T) {
	f := newAuthServer(t)
	id := deployWithCard(t, f, f.key["acme-dev"], "sub", "tenant", "ma.research", "r")
	tok, _ := f.s.Signer.Mint("agent:"+id, agentAudience, "acme", "default", "dev", time.Minute)
	if w, _ := f.do("GET", "/api/v1/a2a/registry/agents?q=r", tok, ""); w.Code != 200 {
		t.Fatalf("agent registry = %d", w.Code)
	}
	if w, _ := f.do("POST", "/api/v1/agents", tok, manifestJSON); w.Code != 403 {
		t.Fatalf("agent must not create agents = %d", w.Code)
	}
	other, _ := f.s.Signer.Mint("agent:x", agentAudience, "beta", "default", "dev", time.Minute)
	if w, _ := f.do("POST", "/api/v1/agents/"+id+"/a2a", other, `{"jsonrpc":"2.0","id":1,"method":"GetTask","params":{}}`); w.Code != 404 {
		t.Fatalf("cross-org agent invoke = %d", w.Code)
	}
}
