package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	"atlas-manager/internal/events"
	"atlas-manager/internal/store"
)

const manifestJSON = `{"apiVersion":"agents.prodai.io/v1alpha1","kind":"AtlasAgent",
 "metadata":{"name":"al-plan","unitId":"AL-PLAN"},
 "spec":{"version":"0.1.0","interfaces":[{"binding":"JSONRPC","port":8000,"path":"/a2a"}]}}`

func newTestServer(t *testing.T) (*Server, *events.Recorder) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	db, err := store.Open("sqlite", "file:"+filepath.Join(t.TempDir(), "t.db"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Migrate(db, "sqlite"); err != nil {
		t.Fatal(err)
	}
	rec := &events.Recorder{}
	return &Server{DB: db, Events: rec, Env: "test", Engine: "sqlite", Proxy: http.DefaultClient}, rec
}

func do(t *testing.T, h http.Handler, method, path, body string) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest(method, path, strings.NewReader(body)))
	var out map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	return w, out
}

func TestAgentLifecycle(t *testing.T) {
	s, rec := newTestServer(t)
	h := s.Router()

	w, created := do(t, h, "POST", "/api/v1/agents", manifestJSON)
	if w.Code != http.StatusCreated {
		t.Fatalf("create = %d %s", w.Code, w.Body)
	}
	id := created["id"].(string)
	if created["layer"] != "L1" || created["currentRev"] != float64(1) {
		t.Fatalf("unexpected agent %v", created)
	}

	if w, _ := do(t, h, "POST", "/api/v1/agents", manifestJSON); w.Code != http.StatusConflict {
		t.Fatalf("duplicate = %d, want 409", w.Code)
	}

	// 같은 spec → no-op
	w, out := do(t, h, "PATCH", "/api/v1/agents/"+id, manifestJSON)
	if w.Code != 200 || out["changed"] != false {
		t.Fatalf("noop patch = %d %v", w.Code, out)
	}
	// spec 변경 → rev 2
	w, out = do(t, h, "PATCH", "/api/v1/agents/"+id, strings.Replace(manifestJSON, `"0.1.0"`, `"0.2.0"`, 1))
	if w.Code != 200 || out["changed"] != true || out["agent"].(map[string]any)["currentRev"] != float64(2) {
		t.Fatalf("patch = %d %v", w.Code, out)
	}
	// 불변 필드
	if w, _ := do(t, h, "PATCH", "/api/v1/agents/"+id, strings.Replace(manifestJSON, `"al-plan"`, `"other"`, 1)); w.Code != 422 {
		t.Fatalf("immutable = %d, want 422", w.Code)
	}

	w, out = do(t, h, "GET", "/api/v1/agents/"+id+"/versions", "")
	if w.Code != 200 || len(out["items"].([]any)) != 2 {
		t.Fatalf("versions = %d %v", w.Code, out)
	}

	if w, _ := do(t, h, "DELETE", "/api/v1/agents/"+id, ""); w.Code != http.StatusNoContent {
		t.Fatalf("delete = %d", w.Code)
	}
	if w, _ := do(t, h, "GET", "/api/v1/agents/"+id, ""); w.Code != 404 {
		t.Fatalf("get deleted = %d, want 404", w.Code)
	}
	// 소프트 삭제된 이름은 재등록 가능
	if w, _ := do(t, h, "POST", "/api/v1/agents", manifestJSON); w.Code != http.StatusCreated {
		t.Fatalf("re-register = %d %s", w.Code, w.Body)
	}

	want := []string{"plt.agent.registered", "plt.agent.updated", "plt.agent.deleted", "plt.agent.registered"}
	if got := rec.Types(); strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("events = %v, want %v", got, want)
	}
}

func TestCreateRejectsInvalidManifest(t *testing.T) {
	s, _ := newTestServer(t)
	w, out := do(t, s.Router(), "POST", "/api/v1/agents", `{"apiVersion":"x","kind":"AtlasAgent","metadata":{"name":"a","unitId":"NOPE"},"spec":{}}`)
	if w.Code != 422 || out["error"] != "invalid_manifest" || len(out["details"].([]any)) == 0 {
		t.Fatalf("got %d %v", w.Code, out)
	}
}

func TestListFiltersByUnit(t *testing.T) {
	s, _ := newTestServer(t)
	h := s.Router()
	do(t, h, "POST", "/api/v1/agents", manifestJSON)
	do(t, h, "POST", "/api/v1/agents", strings.NewReplacer(`"al-plan"`, `"gw-route"`, `"AL-PLAN"`, `"GW-ROUTE"`).Replace(manifestJSON))
	_, out := do(t, h, "GET", "/api/v1/agents?unit=GW-ROUTE", "")
	items := out["items"].([]any)
	if len(items) != 1 || items[0].(map[string]any)["name"] != "gw-route" {
		t.Fatalf("filter = %v", items)
	}
}

// TestOpenAPIMatchesRoutes 는 라우터와 OpenAPI 문서의 경로·메서드가 1:1 인지 확인한다 (COMPLIANCE CMP-02 계약 적합성).
func TestOpenAPIMatchesRoutes(t *testing.T) {
	s, _ := newTestServer(t)
	var doc struct {
		Paths map[string]map[string]any `json:"paths"`
	}
	if err := json.NewDecoder(bytes.NewReader(openapiSpec)).Decode(&doc); err != nil {
		t.Fatal(err)
	}
	spec := map[string]bool{}
	for p, ops := range doc.Paths {
		for m := range ops {
			spec[strings.ToUpper(m)+" "+p] = true
		}
	}
	param := regexp.MustCompile(`[:*](\w+)`)
	routes := map[string]bool{}
	for _, r := range s.Router().Routes() {
		routes[r.Method+" "+param.ReplaceAllString(r.Path, "{$1}")] = true
	}
	for k := range routes {
		if !spec[k] {
			t.Errorf("route %s missing in openapi.json", k)
		}
	}
	for k := range spec {
		if !routes[k] {
			t.Errorf("openapi.json %s has no route", k)
		}
	}
}
