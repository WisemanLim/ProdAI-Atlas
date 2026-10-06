package api

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"atlas-manager/internal/deploy"
	"atlas-manager/internal/store"
	"atlas-manager/internal/ui"
)

// fakeAgent 는 /readyz · Agent Card · /a2a 를 흉내 낸다. ready=false 면 /readyz 503.
func fakeAgent(t *testing.T, name string, ready *atomic.Bool) *httptest.Server {
	t.Helper()
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/readyz":
			if !ready.Load() {
				w.WriteHeader(503)
				return
			}
			w.Write([]byte(`{"status":"ok"}`))
		case "/.well-known/agent-card.json":
			json.NewEncoder(w).Encode(map[string]any{"name": name, "description": "d", "version": "0.1.0", "capabilities": map[string]any{},
				"defaultInputModes": []string{"text/plain"}, "defaultOutputModes": []string{"application/json"},
				"skills":              []map[string]string{{"id": "loop.plan"}},
				"supportedInterfaces": []map[string]string{{"url": srv.URL + "/a2a", "protocolBinding": "JSONRPC"}}})
		case "/a2a":
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{"traceparent":"` + r.Header.Get("traceparent") + `"}}`))
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestDeploymentLifecycleWithProberAndProxy(t *testing.T) {
	s, rec := newTestServer(t)
	h := s.Router()
	_, agent := do(t, h, "POST", "/api/v1/agents", manifestJSON)
	id := agent["id"].(string)

	var ready atomic.Bool
	fa := fakeAgent(t, "al-plan", &ready)
	now := time.Now().UTC()
	p := &deploy.Prober{DB: s.DB, Events: rec, Client: http.DefaultClient, FailThreshold: 2, StartupTimeout: time.Minute, Now: func() time.Time { return now }}

	w, dep := do(t, h, "POST", "/api/v1/deployments", `{"agentId":"`+id+`","endpoint":"`+fa.URL+`/"}`)
	if w.Code != 201 || dep["status"] != "pending" || dep["env"] != "dev" || dep["endpoint"] != fa.URL {
		t.Fatalf("create = %d %v", w.Code, dep)
	}
	depID := dep["id"].(string)
	// 같은 agent·env 는 멱등 갱신
	if w, again := do(t, h, "POST", "/api/v1/deployments", `{"agentId":"`+id+`","endpoint":"`+fa.URL+`"}`); w.Code != 200 || again["id"] != depID {
		t.Fatalf("idempotent = %d %v", w.Code, again)
	}
	// proxy: ready 배포 없음
	if w, _ := do(t, h, "POST", "/api/v1/agents/"+id+"/a2a", `{}`); w.Code != 503 {
		t.Fatalf("proxy before ready = %d", w.Code)
	}

	status := func() string {
		_, d := do(t, h, "GET", "/api/v1/deployments/"+depID, "")
		return d["status"].(string)
	}
	p.Tick(context.Background())
	if st := status(); st != "starting" {
		t.Fatalf("not ready → %s, want starting", st)
	}
	ready.Store(true)
	p.Tick(context.Background())
	_, d := do(t, h, "GET", "/api/v1/deployments/"+depID, "")
	if d["status"] != "ready" || d["card"].(map[string]any)["name"] != "al-plan" {
		t.Fatalf("ready = %v", d)
	}

	// agent-proxy 가 traceparent 를 전달
	req := httptest.NewRequest("POST", "/api/v1/agents/"+id+"/a2a", strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"GetTask"}`))
	req.Header.Set("traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
	pw := httptest.NewRecorder()
	h.ServeHTTP(pw, req)
	if pw.Code != 200 || !strings.Contains(pw.Body.String(), "4bf92f3577b34da6a3ce929d0e0e4736") || pw.Header().Get("x-atlas-deployment") != depID {
		t.Fatalf("proxy = %d %s", pw.Code, pw.Body)
	}

	ready.Store(false)
	p.Tick(context.Background())
	if st := status(); st != "degraded" {
		t.Fatalf("1 fail → %s, want degraded", st)
	}
	p.Tick(context.Background())
	if st := status(); st != "failed" {
		t.Fatalf("2 fails → %s, want failed", st)
	}

	// 에이전트 삭제 → 배포 stopped
	do(t, h, "DELETE", "/api/v1/agents/"+id, "")
	var dd store.Deployment
	s.DB.First(&dd, "id = ?", depID)
	if dd.Status != store.DeployStopped {
		t.Fatalf("after agent delete = %s", dd.Status)
	}

	want := []string{"plt.agent.registered", "plt.deploy.started", "plt.deploy.progress", "plt.deploy.succeeded",
		"plt.deploy.progress", "plt.deploy.progress", "plt.deploy.progress", "plt.agent.deleted"}
	if got := strings.Join(rec.Types(), ","); got != strings.Join(want, ",") {
		t.Fatalf("events =\n%s\nwant\n%s", got, strings.Join(want, ","))
	}
}

func TestProberFailsOnCardMismatchAndStartupTimeout(t *testing.T) {
	s, rec := newTestServer(t)
	h := s.Router()
	_, agent := do(t, h, "POST", "/api/v1/agents", manifestJSON)
	var ready atomic.Bool
	ready.Store(true)
	wrong := fakeAgent(t, "someone-else", &ready)
	_, dep := do(t, h, "POST", "/api/v1/deployments", `{"agentId":"`+agent["id"].(string)+`","endpoint":"`+wrong.URL+`"}`)
	now := time.Now().UTC()
	p := &deploy.Prober{DB: s.DB, Events: rec, Client: http.DefaultClient, FailThreshold: 3, StartupTimeout: time.Minute, Now: func() time.Time { return now }}
	p.Tick(context.Background())
	_, d := do(t, h, "GET", "/api/v1/deployments/"+dep["id"].(string), "")
	if d["status"] != "failed" || !strings.Contains(d["message"].(string), "name") {
		t.Fatalf("card mismatch = %v", d)
	}

	// 기동 시간 초과
	_, agent2 := do(t, h, "POST", "/api/v1/agents", strings.Replace(manifestJSON, `"al-plan"`, `"al-plan-2"`, 1))
	_, dep2 := do(t, h, "POST", "/api/v1/deployments", `{"agentId":"`+agent2["id"].(string)+`","endpoint":"http://127.0.0.1:1"}`)
	now = now.Add(2 * time.Minute)
	p.Tick(context.Background())
	_, d2 := do(t, h, "GET", "/api/v1/deployments/"+dep2["id"].(string), "")
	if d2["status"] != "failed" || !strings.Contains(d2["message"].(string), "startup timeout") {
		t.Fatalf("startup timeout = %v", d2)
	}
}

func TestCreateDeploymentValidation(t *testing.T) {
	s, _ := newTestServer(t)
	h := s.Router()
	_, agent := do(t, h, "POST", "/api/v1/agents", manifestJSON)
	if w, _ := do(t, h, "POST", "/api/v1/deployments", `{"agentId":"`+agent["id"].(string)+`","endpoint":"ftp://x"}`); w.Code != 422 {
		t.Fatalf("bad endpoint = %d", w.Code)
	}
	if w, _ := do(t, h, "POST", "/api/v1/deployments", `{"agentId":"nope","endpoint":"http://x"}`); w.Code != 404 {
		t.Fatalf("missing agent = %d", w.Code)
	}
}

// 다중 워커가 동시에 배포를 등록해도 활성 배포는 (agent, env) 당 1개 (impl 5th 결함 회귀 시험).
func TestConcurrentDeploymentRegistrationIsIdempotent(t *testing.T) {
	s, _ := newTestServer(t)
	h := s.Router()
	_, agent := do(t, h, "POST", "/api/v1/agents", manifestJSON)
	body := `{"agentId":"` + agent["id"].(string) + `","endpoint":"http://agent:8000"}`
	ids := make(chan string, 8)
	for range 8 {
		go func() {
			w := httptest.NewRecorder()
			h.ServeHTTP(w, httptest.NewRequest("POST", "/api/v1/deployments", strings.NewReader(body)))
			var d map[string]any
			_ = json.Unmarshal(w.Body.Bytes(), &d)
			if w.Code != 200 && w.Code != 201 {
				ids <- "ERR " + w.Body.String()
				return
			}
			ids <- d["id"].(string)
		}()
	}
	seen := map[string]bool{}
	for range 8 {
		seen[<-ids] = true
	}
	var active int64
	s.DB.Model(&store.Deployment{}).Where("agent_id = ? AND status <> ?", agent["id"], store.DeployStopped).Count(&active)
	if len(seen) != 1 || active != 1 {
		t.Fatalf("ids=%v active=%d", seen, active)
	}
}

func TestServiceProxyAndUnknownService(t *testing.T) {
	s, _ := newTestServer(t)
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"path":"` + r.URL.Path + `","query":"` + r.URL.RawQuery + `","cookie":"` + r.Header.Get("Cookie") + `"}`))
	}))
	defer up.Close()
	s.Services = ParseServices("ob=" + up.URL + ", bad=::nope")
	h := s.Router()
	srv := httptest.NewServer(h) // ReverseProxy 는 CloseNotifier 가 필요 — 실제 HTTP 서버로 시험
	defer srv.Close()
	req, _ := http.NewRequest("GET", srv.URL+"/svc/ob/v1/traces?q=x", nil)
	req.Header.Set("Cookie", "session=secret")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(res.Body)
	res.Body.Close()
	if res.StatusCode != 200 || !strings.Contains(string(body), `"path":"/v1/traces"`) || !strings.Contains(string(body), `"query":"q=x"`) ||
		strings.Contains(string(body), "secret") {
		t.Fatalf("proxy = %d %s", res.StatusCode, body)
	}
	if w, _ := do(t, h, "GET", "/svc/nope/x", ""); w.Code != 404 {
		t.Fatalf("unknown service = %d", w.Code)
	}
	if len(s.Services) != 1 {
		t.Fatalf("bad entry should be ignored: %v", s.Services)
	}
}

func TestConsoleSPAFallback(t *testing.T) {
	s, _ := newTestServer(t)
	s.UI = ui.Handler()
	h := s.Router()
	for _, p := range []string{"/", "/o/default/p/default/e/dev/runs"} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("GET", p, nil))
		if w.Code != 200 || !strings.Contains(w.Body.String(), "ProdAI Atlas") {
			t.Fatalf("%s = %d %s", p, w.Code, w.Body)
		}
	}
	if w, _ := do(t, h, "GET", "/api/v1/nope", ""); w.Code != 404 || !strings.Contains(w.Body.String(), "not_found") {
		t.Fatalf("api 404 must stay json: %d %s", w.Code, w.Body)
	}
}
