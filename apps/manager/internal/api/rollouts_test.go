package api

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"atlas-manager/internal/deploy"
	"atlas-manager/internal/store"
)

// failingAgent 는 ready·카드는 정상이지만 /a2a 가 500 인 canary 다.
func failingAgent(t *testing.T, name, version string) *httptest.Server {
	t.Helper()
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/readyz":
			w.Write([]byte(`{"status":"ok"}`))
		case "/.well-known/agent-card.json":
			json.NewEncoder(w).Encode(map[string]any{"name": name, "description": "d", "version": version, "capabilities": map[string]any{},
				"defaultInputModes": []string{"text/plain"}, "defaultOutputModes": []string{"application/json"},
				"skills": []map[string]string{{"id": "loop.plan"}}, "supportedInterfaces": []map[string]string{{"url": srv.URL + "/a2a", "protocolBinding": "JSONRPC"}}})
		default:
			w.WriteHeader(500)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func rolloutFixture(t *testing.T) (*Server, http.Handler, string, *deploy.Prober) {
	s, rec := newTestServer(t)
	h := s.Router()
	_, agent := do(t, h, "POST", "/api/v1/agents", manifestJSON)
	now := time.Now().UTC()
	p := &deploy.Prober{DB: s.DB, Events: rec, Client: http.DefaultClient, FailThreshold: 2, StartupTimeout: time.Minute, Now: func() time.Time { return now }}
	return s, h, agent["id"].(string), p
}

func TestCanaryWeightedRoutingPromoteAndStandbyRollback(t *testing.T) {
	s, h, id, p := rolloutFixture(t)
	var up atomic.Bool
	up.Store(true)
	stable, cand := fakeAgent(t, "al-plan", &up), fakeAgent(t, "al-plan", &up)
	do(t, h, "POST", "/api/v1/deployments", `{"agentId":"`+id+`","endpoint":"`+stable.URL+`"}`)
	p.Tick(context.Background())
	if w, bg := do(t, h, "POST", "/api/v1/agents/"+id+"/rollouts", `{"endpoint":"`+cand.URL+`","strategy":"bluegreen"}`); w.Code != 201 || bg["weight"] != float64(0) {
		t.Fatalf("bluegreen green weight = %d %v", w.Code, bg)
	} else {
		do(t, h, "POST", "/api/v1/rollouts/"+bg["id"].(string)+"/abort", `{}`)
	}
	w, ro := do(t, h, "POST", "/api/v1/agents/"+id+"/rollouts", `{"endpoint":"`+cand.URL+`","steps":[30,100]}`)
	if w.Code != 201 || ro["weight"] != float64(30) || ro["status"] != "running" {
		t.Fatalf("rollout = %d %v", w.Code, ro)
	}
	rid := ro["id"].(string)
	if w, _ := do(t, h, "POST", "/api/v1/agents/"+id+"/rollouts", `{"endpoint":"`+cand.URL+`"}`); w.Code != 409 {
		t.Fatalf("second rollout = %d", w.Code)
	}
	if w, _ := do(t, h, "POST", "/api/v1/rollouts/"+rid+"/advance", ``); w.Code != 409 { // canary 아직 pending
		t.Fatalf("advance before ready = %d", w.Code)
	}
	p.Tick(context.Background())
	// 고정 배정 키별로 갈라지고, 같은 키는 같은 쪽
	slots := map[string]int{}
	for i := 0; i < 200; i++ {
		req := httptest.NewRequest("POST", "/api/v1/agents/"+id+"/a2a", strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"GetTask","params":{}}`))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("x-atlas-assign-key", "user-"+itoa(i))
		rw := httptest.NewRecorder()
		h.ServeHTTP(rw, req)
		slots[rw.Header().Get("x-atlas-deployment-slot")]++
	}
	if slots["canary"] < 30 || slots["canary"] > 95 || slots["primary"] < 100 {
		t.Fatalf("weighted split = %v", slots)
	}
	if w, out := do(t, h, "POST", "/api/v1/rollouts/"+rid+"/advance", ``); w.Code != 200 || out["status"] != "promoted" {
		t.Fatalf("promote = %d %v", w.Code, out)
	}
	var deps []store.Deployment
	s.DB.Where("status <> ?", store.DeployStopped).Order("slot").Find(&deps)
	got := map[string]string{}
	for _, d := range deps {
		got[d.Slot] = d.Endpoint
	}
	if got["primary"] != cand.URL || got["standby"] != stable.URL {
		t.Fatalf("after promote = %v", got)
	}
	var prim store.Deployment
	s.DB.Where("slot = ? AND status <> ?", "primary", store.DeployStopped).First(&prim)
	w, rb := do(t, h, "POST", "/api/v1/deployments/"+prim.ID+"/rollback", `{}`)
	if w.Code != 200 || rb["mode"] != "standby_swap" || rb["primary"].(map[string]any)["endpoint"] != stable.URL {
		t.Fatalf("rollback = %d %v", w.Code, rb)
	}
	_, topo := do(t, h, "GET", "/api/v1/topology?env=dev", "")
	if edges := topo["edges"].([]any); len(edges) != 1 || edges[0].(map[string]any)["calls"].(float64) < 200 {
		t.Fatalf("topology = %v", topo)
	}
}

func TestCanaryAutoRollbackOnErrorsAndDrift(t *testing.T) {
	s, h, id, p := rolloutFixture(t)
	var up atomic.Bool
	up.Store(true)
	stable := fakeAgent(t, "al-plan", &up)
	bad := failingAgent(t, "al-plan", "9.9.9")
	do(t, h, "POST", "/api/v1/deployments", `{"agentId":"`+id+`","endpoint":"`+stable.URL+`"}`)
	p.Tick(context.Background())
	_, ro := do(t, h, "POST", "/api/v1/agents/"+id+"/rollouts", `{"endpoint":"`+bad.URL+`","steps":[100],"gate":{"minCalls":3,"maxErrorRate":0.5}}`)
	p.Tick(context.Background())
	var canary store.Deployment
	s.DB.First(&canary, "id = ?", ro["canary"].(map[string]any)["id"])
	if canary.Drift == nil || !strings.Contains(*canary.Drift, "9.9.9") {
		t.Fatalf("drift = %v", canary.Drift)
	}
	for i := 0; i < 4; i++ {
		do(t, h, "POST", "/api/v1/agents/"+id+"/a2a", `{"jsonrpc":"2.0","id":1,"method":"GetTask","params":{}}`)
	}
	s.EvaluateRollouts(context.Background())
	_, out := do(t, h, "GET", "/api/v1/rollouts/"+ro["id"].(string), "")
	if out["status"] != "rolled_back" || !strings.Contains(out["reason"].(string), "오류율") {
		t.Fatalf("auto rollback = %v", out)
	}
	if d, err := pickDeployment(s.DB, id, "dev", "k"); err != nil || d.Endpoint != stable.URL {
		t.Fatalf("after rollback routes to %v %v", d.Endpoint, err)
	}
}

func TestDeployPolicyPromotionApprovalScaleAndK8s(t *testing.T) {
	s, h, id, p := rolloutFixture(t)
	var up atomic.Bool
	up.Store(true)
	fa := fakeAgent(t, "al-plan", &up)
	do(t, h, "POST", "/api/v1/deployments", `{"agentId":"`+id+`","endpoint":"`+fa.URL+`"}`)
	p.Tick(context.Background())
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	der, _ := x509.MarshalPKIXPublicKey(pub)
	pemKey := string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}))
	pol, _ := json.Marshal(map[string]any{"requireDigest": true, "allowedRegistries": []string{"ghcr.io/prodai"}, "requireSignature": true,
		"trustedKeys": map[string]string{"k1": pemKey}, "requireApproval": true})
	if w, _ := do(t, h, "PUT", "/api/v1/orgs/default/deploy-policies/staging", string(pol)); w.Code != 200 {
		t.Fatalf("policy = %d", w.Code)
	}
	// 현재 리비전은 digest·서명 없음 → 승격 거부
	w, out := do(t, h, "POST", "/api/v1/agents/"+id+"/promote", `{"from":"dev","to":"staging"}`)
	if w.Code != 422 || len(out["details"].(map[string]any)["violations"].([]any)) != 3 {
		t.Fatalf("policy deny = %d %v", w.Code, out)
	}
	// 서명된 새 리비전 → 배포 → 승격은 승인함으로
	raw := map[string]any{}
	_ = json.Unmarshal([]byte(manifestJSON), &raw)
	spec := raw["spec"].(map[string]any)
	spec["version"] = "0.2.0"
	spec["artifacts"] = map[string]any{"image": "ghcr.io/prodai/al-plan@sha256:" + strings.Repeat("a", 64)}
	sig := ed25519.Sign(priv, SignedManifestMessage(unsignedSpecHash(raw)))
	spec["artifacts"].(map[string]any)["signature"] = map[string]any{"kid": "k1", "sig": base64.StdEncoding.EncodeToString(sig)}
	mb, _ := json.Marshal(raw)
	if w, _ := do(t, h, "PATCH", "/api/v1/agents/"+id, string(mb)); w.Code != 200 {
		t.Fatalf("patch = %d", w.Code)
	}
	do(t, h, "POST", "/api/v1/deployments", `{"agentId":"`+id+`","endpoint":"`+fa.URL+`"}`)
	p.Tick(context.Background())
	w, out = do(t, h, "POST", "/api/v1/agents/"+id+"/promote", `{"from":"dev","to":"staging"}`)
	if w.Code != 202 || out["status"] != "pending_approval" {
		t.Fatalf("promote = %d %v", w.Code, out)
	}
	apID := out["approvalId"].(string)
	if w, d := do(t, h, "POST", "/api/v1/approvals/"+apID+"/decision", `{"decision":"approve","reason":"ok"}`); w.Code != 200 || !strings.HasPrefix(d["callbackResult"].(string), "promoted:") {
		t.Fatalf("decision = %d %v", w.Code, d)
	}
	var st store.Deployment
	if s.DB.First(&st, "env = ? AND slot = ?", "staging", "primary").Error != nil || st.Rev != 2 {
		t.Fatalf("staging = %+v", st)
	}
	if w, _ := do(t, h, "POST", "/api/v1/agents/"+id+"/promote", `{"from":"dev","to":"prod"}`); w.Code != 422 {
		t.Fatalf("skip-level promote = %d", w.Code)
	}
	w, sc := do(t, h, "PUT", "/api/v1/deployments/"+st.ID+"/scale", `{"replicas":3}`)
	if w.Code != 200 || sc["desired"] != float64(3) || sc["applied"] != false {
		t.Fatalf("scale = %d %v", w.Code, sc)
	}
	rw := httptest.NewRecorder()
	h.ServeHTTP(rw, httptest.NewRequest("GET", "/api/v1/deployments/"+st.ID+"/manifests", nil))
	y := rw.Body.String()
	for _, want := range []string{"kind: Deployment", "kind: Service", "replicas: 3", "readOnlyRootFilesystem: true", "namespace: atlas-staging", "ghcr.io/prodai/al-plan@sha256:"} {
		if !strings.Contains(y, want) {
			t.Fatalf("k8s manifest missing %q:\n%s", want, y)
		}
	}
}
