package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"atlas-gateway/internal/audit"
	"atlas-gateway/internal/auth"
	"atlas-gateway/internal/config"
	"atlas-gateway/internal/cost"
	"atlas-gateway/internal/events"
	"atlas-gateway/internal/guard"
	"atlas-gateway/internal/provider"
	"atlas-gateway/internal/ratelimit"
	"atlas-gateway/internal/router"
)

type fixture struct {
	h     http.Handler
	s     *Server
	audit *bytes.Buffer
	rec   *events.Recorder
	key   string
}

const admin = "test-admin"

func setup(t *testing.T, vk auth.VirtualKey) *fixture {
	t.Helper()
	gin.SetMode(gin.TestMode)
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	cfg, err := config.Load("")
	if err != nil {
		t.Fatal(err)
	}
	cfg.Retry.BaseMS, cfg.Retry.MaxMS = 1, 1
	prices, _ := cost.NewTable(cfg.Prices)
	buf, rec := &bytes.Buffer{}, &events.Recorder{}
	s := &Server{Cfg: cfg, Redis: rdb, Keys: auth.NewStore(rdb, "test"), Limiter: ratelimit.New(rdb),
		Routes: router.New(cfg, map[string]provider.Provider{"mock": provider.Mock{}}), Prices: prices,
		Audit: audit.New(buf), Events: rec, Pools: map[string]*provider.KeyPool{}, AdminToken: admin, Env: "test", Now: time.Now}
	f := &fixture{h: s.Router(), s: s, audit: buf, rec: rec}
	if vk.Tenant != "" {
		f.key = "sk-atlas-test-abcdefghijklmnopqrstuv"
		if err := s.Keys.Put(t.Context(), f.key, vk); err != nil {
			t.Fatal(err)
		}
	}
	return f
}

func (f *fixture) do(method, path, key, body string, hdr ...string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	for i := 0; i+1 < len(hdr); i += 2 {
		req.Header.Set(hdr[i], hdr[i+1])
	}
	w := httptest.NewRecorder()
	f.h.ServeHTTP(w, req)
	return w
}

func errCode(w *httptest.ResponseRecorder) string {
	var out struct {
		Error struct{ Code string } `json:"error"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	return out.Error.Code
}

const chatBody = `{"model":"chat-default","messages":[{"role":"user","content":"안녕"}]}`

func TestChatHappyPathWithCostAuditEvent(t *testing.T) {
	f := setup(t, auth.VirtualKey{Tenant: "t1", Models: []string{"*"}, RPM: 100, TPM: 100000})
	w := f.do("POST", "/v1/chat/completions", f.key, chatBody, "x-atlas-tags", "agent_id:al-plan,secret:x")
	if w.Code != 200 {
		t.Fatalf("code=%d body=%s", w.Code, w.Body)
	}
	var out struct {
		Choices []struct{ Message struct{ Content string } } `json:"choices"`
		Usage   struct {
			TotalTokens int `json:"total_tokens"`
		} `json:"usage"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	if out.Choices[0].Message.Content != "[mock:mock-chat] 안녕" || out.Usage.TotalTokens == 0 {
		t.Fatalf("out=%s", w.Body)
	}
	if !regexp.MustCompile(`^0\.\d{8}$`).MatchString(w.Header().Get("x-atlas-cost-usd")) || w.Header().Get("x-atlas-route") != "mock/mock-chat" {
		t.Fatalf("headers=%v", w.Header())
	}
	if n, err := audit.Verify(f.audit); err != nil || n != 1 {
		t.Fatalf("audit n=%d err=%v", n, err)
	}
	time.Sleep(50 * time.Millisecond)
	if got := f.rec.Types(); len(got) != 1 || got[0] != "gw.request.completed" {
		t.Fatalf("events=%v", got)
	}
	tags := f.rec.Events[0].Data.(gin.H)["tags"].(map[string]string)
	if tags["agent_id"] != "al-plan" || tags["secret"] != "" {
		t.Fatalf("tags=%v (whitelist)", tags)
	}
	vk, _ := f.s.Keys.Lookup(t.Context(), f.key)
	u := f.do("GET", "/admin/usage?key_prefix="+vk.Prefix, admin, "")
	if !strings.Contains(u.Body.String(), `"requests":1`) {
		t.Fatalf("usage=%s", u.Body)
	}
}

func TestAuthErrors(t *testing.T) {
	past := time.Now().Add(-time.Hour)
	f := setup(t, auth.VirtualKey{Tenant: "t1", Models: []string{"*"}, ExpiresAt: &past})
	if w := f.do("POST", "/v1/chat/completions", "", chatBody); w.Code != 401 || errCode(w) != "invalid_api_key" {
		t.Fatalf("no key: %d %s", w.Code, w.Body)
	}
	if w := f.do("POST", "/v1/chat/completions", f.key, chatBody); w.Code != 401 || errCode(w) != "expired_api_key" {
		t.Fatalf("expired: %d %s", w.Code, w.Body)
	}
}

func TestPolicyDecisions(t *testing.T) {
	f := setup(t, auth.VirtualKey{Tenant: "t1", Models: []string{"chat-*", "flaky-*"}})
	cases := []struct {
		body, code string
		status     int
	}{
		{`{"model":"planner-default","messages":[{"role":"user","content":"x"}]}`, "model_not_allowed", 403},
		{`{"model":"nope","messages":[{"role":"user","content":"x"}]}`, "model_not_found", 404},
		{`{"model":"chat-default","messages":[{"role":"user","content":"x"}],"logit_bias":{"1":1}}`, "param_not_allowed", 400},
		{`{"model":"chat-default"}`, "missing_field", 400},
	}
	for _, c := range cases {
		if w := f.do("POST", "/v1/chat/completions", f.key, c.body); w.Code != c.status || errCode(w) != c.code {
			t.Errorf("%s → %d %s", c.code, w.Code, w.Body)
		}
	}
	w := f.do("POST", "/v1/chat/completions", f.key, `{"model":"chat-default","max_tokens":99999,"messages":[{"role":"user","content":"x"}]}`)
	if w.Code != 200 || w.Header().Get("x-atlas-policy-mutations") != "max_tokens:99999->4096" {
		t.Fatalf("mutation: %d %v", w.Code, w.Header())
	}
	if n, err := audit.Verify(f.audit); err != nil || n != 5 { // deny 4 + allow 1
		t.Fatalf("audit n=%d err=%v", n, err)
	}
}

func TestRPMLimit(t *testing.T) {
	f := setup(t, auth.VirtualKey{Tenant: "t1", Models: []string{"*"}, RPM: 2})
	for i := 0; i < 2; i++ {
		if w := f.do("POST", "/v1/chat/completions", f.key, chatBody); w.Code != 200 {
			t.Fatalf("req %d: %d", i, w.Code)
		}
	}
	w := f.do("POST", "/v1/chat/completions", f.key, chatBody)
	if w.Code != 429 || errCode(w) != "rate_limit_exceeded" || w.Header().Get("Retry-After") == "" {
		t.Fatalf("3rd: %d %v %s", w.Code, w.Header(), w.Body)
	}
}

func TestTPMLimit(t *testing.T) {
	f := setup(t, auth.VirtualKey{Tenant: "t1", Models: []string{"*"}, TPM: 600})
	body := `{"model":"chat-default","max_tokens":500,"messages":[{"role":"user","content":"x"}]}`
	if w := f.do("POST", "/v1/chat/completions", f.key, body); w.Code != 200 {
		t.Fatalf("1st: %d %s", w.Code, w.Body)
	}
	// 1차는 실사용(7)으로 정산됨 → 2차 예약 2+595 를 더하면 604 > 600
	big := `{"model":"chat-default","max_tokens":595,"messages":[{"role":"user","content":"x"}]}`
	if w := f.do("POST", "/v1/chat/completions", f.key, big); w.Code != 429 || errCode(w) != "tpm_exceeded" {
		t.Fatalf("2nd: %d %s", w.Code, w.Body)
	}
}

func TestFallbackAndUpstream429(t *testing.T) {
	f := setup(t, auth.VirtualKey{Tenant: "t1", Models: []string{"*"}})
	w := f.do("POST", "/v1/chat/completions", f.key, `{"model":"flaky-chat","messages":[{"role":"user","content":"x"}]}`)
	if w.Code != 200 || w.Header().Get("x-atlas-fallback") != "chat-default" {
		t.Fatalf("fallback: %d %v", w.Code, w.Header())
	}
	w = f.do("POST", "/v1/chat/completions", f.key, `{"model":"limited-chat","messages":[{"role":"user","content":"x"}]}`)
	if w.Code != 429 || errCode(w) != "upstream_error" {
		t.Fatalf("upstream 429: %d %s", w.Code, w.Body)
	}
}

func TestStreamingSSE(t *testing.T) {
	f := setup(t, auth.VirtualKey{Tenant: "t1", Models: []string{"*"}})
	w := f.do("POST", "/v1/chat/completions", f.key, `{"model":"chat-default","stream":true,"messages":[{"role":"user","content":"스트리밍 응답을 확인한다"}]}`)
	body := w.Body.String()
	if w.Header().Get("Content-Type") != "text/event-stream" || !strings.HasSuffix(body, "data: [DONE]\n\n") {
		t.Fatalf("sse: %v\n%s", w.Header(), body)
	}
	var content strings.Builder
	var sawUsage bool
	for _, line := range strings.Split(body, "\n\n") {
		data, ok := strings.CutPrefix(line, "data: ")
		if !ok || data == "[DONE]" {
			continue
		}
		var ch struct {
			Choices []struct{ Delta struct{ Content string } } `json:"choices"`
			Usage   *struct {
				TotalTokens int `json:"total_tokens"`
			} `json:"usage"`
		}
		if err := json.Unmarshal([]byte(data), &ch); err != nil {
			t.Fatalf("bad chunk %q", data)
		}
		for _, c := range ch.Choices {
			content.WriteString(c.Delta.Content)
		}
		sawUsage = sawUsage || (ch.Usage != nil && ch.Usage.TotalTokens > 0)
	}
	if content.String() != "[mock:mock-chat] 스트리밍 응답을 확인한다" || !sawUsage {
		t.Fatalf("content=%q usage=%v", content.String(), sawUsage)
	}
}

func TestAdminKeysLifecycle(t *testing.T) {
	f := setup(t, auth.VirtualKey{})
	if w := f.do("POST", "/admin/keys", "wrong", `{"tenant":"t"}`); w.Code != 401 {
		t.Fatalf("admin auth: %d", w.Code)
	}
	w := f.do("POST", "/admin/keys", admin, `{"tenant":"t2","models":["chat-*"],"rpm":10}`)
	var issued struct {
		Key        string          `json:"key"`
		VirtualKey auth.VirtualKey `json:"virtualKey"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &issued)
	if w.Code != 201 || !regexp.MustCompile(`^sk-atlas-test-[0-9A-Za-z]{22}$`).MatchString(issued.Key) {
		t.Fatalf("issue: %d %s", w.Code, w.Body)
	}
	m := f.do("GET", "/v1/models", issued.Key, "")
	if !strings.Contains(m.Body.String(), `"chat-default"`) || strings.Contains(m.Body.String(), "planner-default") {
		t.Fatalf("models filter: %s", m.Body)
	}
	if l := f.do("GET", "/admin/keys", admin, ""); strings.Contains(l.Body.String(), issued.Key) {
		t.Fatal("plaintext key leaked in list")
	}
	if d := f.do("DELETE", "/admin/keys/"+issued.VirtualKey.Prefix, admin, ""); d.Code != 204 {
		t.Fatalf("revoke: %d", d.Code)
	}
	if w := f.do("GET", "/v1/models", issued.Key, ""); w.Code != 401 {
		t.Fatalf("revoked key still works: %d", w.Code)
	}
}

func TestOpenAPIMatchesRoutes(t *testing.T) {
	f := setup(t, auth.VirtualKey{})
	var doc struct {
		Paths map[string]map[string]any `json:"paths"`
	}
	_ = json.Unmarshal(openapiSpec, &doc)
	spec, routes := map[string]bool{}, map[string]bool{}
	for p, ops := range doc.Paths {
		for m := range ops {
			spec[strings.ToUpper(m)+" "+p] = true
		}
	}
	param := regexp.MustCompile(`:(\w+)`)
	for _, r := range f.s.Router().Routes() {
		routes[r.Method+" "+param.ReplaceAllString(r.Path, "{$1}")] = true
	}
	for k := range routes {
		if !spec[k] {
			t.Errorf("route %s missing in openapi.json", k)
		}
	}
	for k := range spec {
		if !routes[k] {
			t.Errorf("openapi %s has no route", k)
		}
	}
}

// 전역 tracer 는 처음 설정된 provider 에 고정되므로 시험 전체가 recorder 하나를 공유한다.
var (
	spanRec = tracetest.NewSpanRecorder()
	tpOnce  sync.Once
)

func tracing() *tracetest.SpanRecorder {
	tpOnce.Do(func() {
		otel.SetTracerProvider(sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(spanRec)))
		otel.SetTextMapPropagator(propagation.TraceContext{})
	})
	return spanRec
}

func lastSpan(sr *tracetest.SpanRecorder) sdktrace.ReadOnlySpan {
	ended := sr.Ended()
	return ended[len(ended)-1]
}

func TestChatJoinsCallerTrace(t *testing.T) {
	sr := tracing()
	f := setup(t, auth.VirtualKey{Tenant: "t1", Models: []string{"*"}})
	parent := "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
	w := f.do("POST", "/v1/chat/completions", f.key, chatBody, "traceparent", parent, "x-atlas-tags", "unit_id:AL-PLAN")
	if w.Code != 200 || w.Header().Get("x-atlas-trace-id") != "4bf92f3577b34da6a3ce929d0e0e4736" {
		t.Fatalf("code=%d headers=%v", w.Code, w.Header())
	}
	var sp sdktrace.ReadOnlySpan
	for _, s := range sr.Ended() {
		if s.SpanContext().TraceID().String() == "4bf92f3577b34da6a3ce929d0e0e4736" {
			sp = s
		}
	}
	if sp == nil {
		t.Fatal("span for caller trace not recorded")
	}
	attrs := map[string]string{}
	for _, a := range sp.Attributes() {
		attrs[string(a.Key)] = a.Value.Emit()
	}
	if sp.Name() != "chat chat-default" || sp.Parent().SpanID().String() != "00f067aa0ba902b7" ||
		attrs["gen_ai.provider.name"] != "mock" || attrs["gen_ai.usage.input_tokens"] == "" || attrs["atlas.unit_id"] != "AL-PLAN" || attrs["atlas.cost.usd"] == "" {
		t.Fatalf("span=%s parent=%s attrs=%v", sp.Name(), sp.Parent().SpanID(), attrs)
	}
}

func TestToolCallingPassThrough(t *testing.T) {
	f := setup(t, auth.VirtualKey{Tenant: "t1", Models: []string{"*"}})
	body := `{"model":"agent-default","tools":[{"type":"function","function":{"name":"atlas_echo","parameters":{"type":"object"}}}],
	 "messages":[{"role":"user","content":"atlas_echo 호출"}]}`
	w := f.do("POST", "/v1/chat/completions", f.key, body)
	var out struct {
		Choices []struct {
			Message struct {
				ToolCalls []provider.ToolCall `json:"tool_calls"`
			} `json:"message"`
			FinishReason string `json:"finish_reason"`
		} `json:"choices"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	if w.Code != 200 || out.Choices[0].FinishReason != "tool_calls" || out.Choices[0].Message.ToolCalls[0].Function.Name != "atlas_echo" {
		t.Fatalf("code=%d body=%s", w.Code, w.Body)
	}
	tc := out.Choices[0].Message.ToolCalls[0]
	follow := `{"model":"agent-default","tools":[{"type":"function","function":{"name":"atlas_echo"}}],"messages":[
	 {"role":"user","content":"atlas_echo 호출"},{"role":"assistant","content":"","tool_calls":[{"id":"` + tc.ID + `","type":"function","function":{"name":"atlas_echo","arguments":"{}"}}]},
	 {"role":"tool","tool_call_id":"` + tc.ID + `","content":"pong"}]}`
	w = f.do("POST", "/v1/chat/completions", f.key, follow)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"[mock:final] pong"`) {
		t.Fatalf("follow-up: %d %s", w.Code, w.Body)
	}
}

// fakeGuard 는 내용에 따라 판정한다: "ATTACK" → block, "010-" → transform(마스킹), "UNSAFE_OUT" → 출력 block.
func fakeGuard(t *testing.T) *httptest.Server {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			Boundary string   `json:"boundary"`
			Contents []string `json:"contents"`
		}
		_ = json.NewDecoder(r.Body).Decode(&in)
		out := map[string]any{"verdict_id": "v-1", "decision": "allow"}
		joined := strings.Join(in.Contents, "\n")
		switch {
		case strings.Contains(joined, "ATTACK"):
			out["decision"], out["reasons"] = "block", []string{"jailbreak: ignore_instructions"}
		case strings.Contains(joined, "UNSAFE_OUT") && in.Boundary == "output":
			out["decision"] = "block"
		case strings.Contains(joined, "010-"):
			masked := make([]string, len(in.Contents))
			for i, c := range in.Contents {
				masked[i] = strings.ReplaceAll(c, "010-1234-5678", "***-****-5678")
			}
			out["decision"], out["contents"] = "transform", masked
		}
		_ = json.NewEncoder(w).Encode(out)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestGuardrailHooks(t *testing.T) {
	f := setup(t, auth.VirtualKey{Tenant: "t1", Models: []string{"*"}})
	f.s.Guard = guard.New(fakeGuard(t).URL, time.Second, true)
	msg := func(c string) string {
		return `{"model":"chat-default","messages":[{"role":"user","content":"` + c + `"}]}`
	}
	w := f.do("POST", "/v1/chat/completions", f.key, msg("ATTACK now"))
	if w.Code != 400 || errCode(w) != "guard_blocked" || w.Header().Get("x-atlas-guard-verdict") != "v-1" {
		t.Fatalf("input block: %d %s", w.Code, w.Body)
	}
	w = f.do("POST", "/v1/chat/completions", f.key, msg("연락처 010-1234-5678"))
	if w.Code != 200 || !strings.Contains(w.Body.String(), "***-****-5678") || strings.Contains(w.Body.String(), "010-1234-5678") ||
		w.Header().Get("x-atlas-guard-input") != "transform" {
		t.Fatalf("input transform: %d %s", w.Code, w.Body)
	}
	w = f.do("POST", "/v1/chat/completions", f.key, msg("UNSAFE_OUT"))
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"finish_reason":"content_filter"`) || w.Header().Get("x-atlas-guard-output") != "block" {
		t.Fatalf("output block: %d %s", w.Code, w.Body)
	}
	if n, err := audit.Verify(f.audit); err != nil || n != 3 {
		t.Fatalf("audit n=%d err=%v", n, err)
	}
}

func TestGuardUnavailablePolicies(t *testing.T) {
	f := setup(t, auth.VirtualKey{Tenant: "t1", Models: []string{"*"}})
	f.s.Guard = guard.New("http://127.0.0.1:1", 200*time.Millisecond, true)
	if w := f.do("POST", "/v1/chat/completions", f.key, chatBody); w.Code != 200 || w.Header().Get("x-atlas-guard-input") != "unavailable" {
		t.Fatalf("fail_open: %d %v", w.Code, w.Header())
	}
	f.s.Guard.FailOpen = false
	if w := f.do("POST", "/v1/chat/completions", f.key, chatBody); w.Code != 503 || errCode(w) != "guard_unavailable" {
		t.Fatalf("fail_closed: %d %s", w.Code, w.Body)
	}
}

func TestCaptureBodiesOnSpan(t *testing.T) {
	sr := tracing()
	f := setup(t, auth.VirtualKey{Tenant: "t1", Models: []string{"*"}})
	f.s.Capture = "redacted"
	w := f.do("POST", "/v1/chat/completions", f.key, `{"model":"chat-default","messages":[{"role":"user","content":"my key AKIAABCDEFGHIJKLMNOP"}]}`)
	if w.Code != 200 {
		t.Fatalf("code=%d", w.Code)
	}
	sp := lastSpan(sr)
	attrs := map[string]string{}
	for _, a := range sp.Attributes() {
		attrs[string(a.Key)] = a.Value.Emit()
	}
	if !strings.Contains(attrs["gen_ai.input.messages"], "[SECRET]") || strings.Contains(attrs["gen_ai.input.messages"], "AKIA") ||
		!strings.Contains(attrs["gen_ai.output.messages"], "assistant") || attrs["atlas.ob.capture"] != "redacted" {
		t.Fatalf("attrs=%v", attrs)
	}
	if len(sp.Events()) == 0 || sp.Events()[0].Name != "atlas.ob.secret_leak_detected" {
		t.Fatalf("events=%v", sp.Events())
	}
	time.Sleep(50 * time.Millisecond)
	if !strings.Contains(strings.Join(f.rec.Types(), ","), "gw.secret_leak_detected") {
		t.Fatalf("nats events=%v", f.rec.Types())
	}
	f.s.Capture = "metadata"
	f.do("POST", "/v1/chat/completions", f.key, chatBody)
	for _, a := range lastSpan(sr).Attributes() {
		if a.Key == "gen_ai.input.messages" {
			t.Fatal("metadata mode captured body")
		}
	}
}
