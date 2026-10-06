package server

import (
	"context"
	"encoding/json"
	"fmt"
	"math/big"
	"strings"
	"testing"
	"time"

	"atlas-gateway/internal/auth"
	"atlas-gateway/internal/econ"
	"atlas-gateway/internal/events"
	"atlas-gateway/internal/provider"
)

func econSetup(t *testing.T) *fixture {
	f := setup(t, auth.VirtualKey{Tenant: "acme", Project: "p1", Models: []string{"*"}})
	pub := func(ctx context.Context, s string, d any) { _ = f.rec.Publish(ctx, s, d) }
	f.s.Budgets = &econ.Budgets{R: f.s.Redis, Now: time.Now, SoftPct: 80, Publish: pub}
	f.s.Tasks = &econ.Tasks{R: f.s.Redis, Default: 50000, SoftPct: 80, Publish: pub}
	f.s.Batches = &econ.Batches{R: f.s.Redis, Cfg: f.s.Cfg, Resolve: f.s.Routes.Resolve,
		Providers: map[string]provider.BatchProvider{"mock": &provider.MockBatch{}}, Prices: f.s.Prices.Cost,
		Discount: big.NewRat(1, 2), Now: time.Now, PollBase: time.Millisecond, PollMax: time.Millisecond, MaxRows: 100, Publish: pub, OnCost: f.s.BatchCost}
	return f
}

func econBody(model, user string, extra string) string {
	return fmt.Sprintf(`{"model":%q,"messages":[{"role":"user","content":%q}]%s}`, model, user, extra)
}

func subjects(rec *events.Recorder) string {
	return strings.Join(rec.Types(), ",")
}

func hasSpanWithInput(t *testing.T, traceID string) bool {
	t.Helper()
	for _, sp := range tracing().Ended() {
		if sp.SpanContext().TraceID().String() != traceID {
			continue
		}
		for _, a := range sp.Attributes() {
			if string(a.Key) == "gen_ai.input.messages" && a.Value.AsString() != "" {
				return true
			}
		}
	}
	return false
}

func TestExactCacheHit(t *testing.T) {
	tracing()
	f := econSetup(t)
	f.s.Capture = "redacted"
	body := econBody("chat-default", "같은 질문", `,"temperature":0`)
	w1 := f.do("POST", "/v1/chat/completions", f.key, body)
	if w1.Code != 200 || w1.Header().Get("x-atlas-cache") != "MISS" {
		t.Fatalf("first: %d %v", w1.Code, w1.Header())
	}
	w2 := f.do("POST", "/v1/chat/completions", f.key, body)
	if w2.Header().Get("x-atlas-cache") != "HIT-EXACT" || w2.Header().Get("x-atlas-cost-usd") != "0.00000000" {
		t.Fatalf("second should hit: %v", w2.Header())
	}
	var a, b map[string]any
	_ = json.Unmarshal(w1.Body.Bytes(), &a)
	_ = json.Unmarshal(w2.Body.Bytes(), &b)
	if fmt.Sprint(a["choices"]) != fmt.Sprint(b["choices"]) {
		t.Fatal("cached content differs")
	}
	if w := f.do("POST", "/v1/chat/completions", f.key, body, "Cache-Control", "no-cache"); w.Header().Get("x-atlas-cache") != "BYPASS" {
		t.Fatalf("no-cache: %v", w.Header())
	}
	if w := f.do("POST", "/v1/chat/completions", f.key, econBody("chat-default", "같은 질문", `,"temperature":0.7`)); w.Header().Get("x-atlas-cache") != "SKIP-TEMPERATURE" {
		t.Fatalf("temperature>0 must skip: %v", w.Header())
	}
	if !hasSpanWithInput(t, w2.Header().Get("x-atlas-trace-id")) {
		t.Fatal("cache hit span must carry captured input (golden-set promotion)")
	}
	k := f.do("GET", "/admin/econ/cache?tenant=acme", admin, "")
	if !strings.Contains(k.Body.String(), `"exact_hits":1`) {
		t.Fatalf("kpi: %s", k.Body.String())
	}
}

func TestPromptCacheReadOnSecondCall(t *testing.T) {
	f := econSetup(t)
	sys := strings.Repeat("고정 시스템 지침과 도구 설명. ", 400)
	body := func(q string) string {
		return fmt.Sprintf(`{"model":"chat-default","messages":[{"role":"system","content":%q},{"role":"user","content":%q}]}`, sys, q)
	}
	w1 := f.do("POST", "/v1/chat/completions", f.key, body("질문 하나 cache-test-"+t.Name()))
	w2 := f.do("POST", "/v1/chat/completions", f.key, body("질문 둘"))
	if w1.Header().Get("x-atlas-prompt-cache") == "" || w2.Header().Get("x-atlas-cache-read-tokens") == "0" {
		t.Fatalf("prompt cache: %v / %v", w1.Header(), w2.Header())
	}
	var r struct {
		Usage struct {
			Details struct {
				Cached int `json:"cached_tokens"`
			} `json:"prompt_tokens_details"`
		} `json:"usage"`
	}
	_ = json.Unmarshal(w2.Body.Bytes(), &r)
	if r.Usage.Details.Cached == 0 {
		t.Fatalf("usage.prompt_tokens_details: %s", w2.Body.String())
	}
	if !strings.HasPrefix(w2.Header().Get("x-atlas-cache-savings-usd"), "0.") || strings.HasPrefix(w2.Header().Get("x-atlas-cache-savings-usd"), "-") {
		t.Fatalf("read should save: %s", w2.Header().Get("x-atlas-cache-savings-usd"))
	}
	k := f.do("GET", "/admin/econ/cache?tenant=acme", admin, "")
	var kpi struct {
		Data []econ.CacheKPI `json:"data"`
	}
	_ = json.Unmarshal(k.Body.Bytes(), &kpi)
	if len(kpi.Data) != 1 || kpi.Data[0].HitRatio <= 0.3 || kpi.Data[0].CacheWrite == 0 {
		t.Fatalf("kpi: %s", k.Body.String())
	}
}

func TestBudgetsBlockDowngradeWarn(t *testing.T) {
	f := econSetup(t)
	put := func(id, body string) {
		if w := f.do("PUT", "/admin/budgets/"+id, admin, body); w.Code != 200 {
			t.Fatalf("put %s: %d %s", id, w.Code, w.Body.String())
		}
	}
	if w := f.do("PUT", "/admin/budgets/bad", admin, `{"scope":"org","scope_id":"acme","amount_usd":"-1","period":"daily"}`); w.Code != 400 {
		t.Fatalf("invalid budget accepted: %d", w.Code)
	}
	var keys struct {
		Items []auth.VirtualKey `json:"items"`
	}
	_ = json.Unmarshal(f.do("GET", "/admin/keys", admin, "").Body.Bytes(), &keys)
	prefix := keys.Items[0].Prefix
	put("acme-key", `{"scope":"key","scope_id":"`+prefix+`","amount_usd":"0.000001","period":"daily","hard_action":"downgrade","downgrade_alias":"chat-small"}`)
	f.do("POST", "/v1/chat/completions", f.key, econBody("chat-large", strings.Repeat("비싼 요청 ", 50), ""))
	w := f.do("POST", "/v1/chat/completions", f.key, econBody("chat-large", "다음 요청", ""))
	if w.Code != 200 || w.Header().Get("x-atlas-route") != "mock/mock-chat-small" || !strings.Contains(w.Header().Get("x-atlas-policy-mutations"), "budget_downgrade:acme-key") {
		t.Fatalf("downgrade: %d %v", w.Code, w.Header())
	}
	put("acme-org", `{"scope":"org","scope_id":"acme","amount_usd":"0.000002","period":"monthly","hard_action":"block"}`)
	f.do("POST", "/v1/chat/completions", f.key, econBody("chat-large", strings.Repeat("또 비싼 요청 ", 50), ""))
	w = f.do("POST", "/v1/chat/completions", f.key, econBody("chat-default", "막혀야 함", ""))
	if w.Code != 429 || !strings.Contains(w.Body.String(), "budget_exceeded") || w.Header().Get("x-atlas-budget-warning") == "" {
		t.Fatalf("block: %d %s %v", w.Code, w.Body.String(), w.Header())
	}
	l := f.do("GET", "/admin/budgets", admin, "")
	if !strings.Contains(l.Body.String(), `"state":"exceeded"`) {
		t.Fatalf("list: %s", l.Body.String())
	}
	if !strings.Contains(subjects(f.rec), "gw.budget.state_changed") {
		t.Fatalf("state event missing: %s", subjects(f.rec))
	}
	if d := f.do("DELETE", "/admin/budgets/acme-org", admin, ""); d.Code != 204 {
		t.Fatalf("delete: %d", d.Code)
	}
	if w := f.do("POST", "/v1/chat/completions", f.key, econBody("chat-default", "다시 허용", "")); w.Code != 200 {
		t.Fatalf("after delete: %d", w.Code)
	}
}

func TestTaskBudgetExceeded(t *testing.T) {
	f := econSetup(t)
	h := []string{"x-atlas-task-id", "task-42", "x-atlas-task-budget", "40"}
	w := f.do("POST", "/v1/chat/completions", f.key, econBody("chat-default", strings.Repeat("긴 입력 ", 30), ""), h...)
	if w.Code != 200 || !strings.HasSuffix(w.Header().Get("x-atlas-task-tokens"), "/40") {
		t.Fatalf("first: %d %v", w.Code, w.Header())
	}
	w = f.do("POST", "/v1/chat/completions", f.key, econBody("chat-default", "또", ""), h...)
	if w.Code != 429 || !strings.Contains(w.Body.String(), "task_budget_exceeded") {
		t.Fatalf("second must stop: %d %s", w.Code, w.Body.String())
	}
	if !strings.Contains(subjects(f.rec), "gw.budget.task_near_limit") {
		t.Fatalf("near-limit event: %s", subjects(f.rec))
	}
	u := f.do("GET", "/admin/econ/tasks/task-42", admin, "")
	if !strings.Contains(u.Body.String(), `"used_tokens"`) {
		t.Fatalf("task usage: %s", u.Body.String())
	}
}

func TestModelPolicyEffortAndRouting(t *testing.T) {
	f := econSetup(t)
	w := f.do("POST", "/v1/chat/completions", f.key, econBody("chat-large", "분류해줘", `,"max_tokens":500`), "x-atlas-task-type", "classify")
	if w.Header().Get("x-atlas-route") != "mock/mock-chat-small" || !strings.Contains(w.Header().Get("x-atlas-policy-mutations"), "max_tokens:500->64") {
		t.Fatalf("classify policy: %v", w.Header())
	}
	w = f.do("POST", "/v1/chat/completions", f.key, econBody("reasoner", "증명", `,"atlas":{"effort":"high","task_type":"code"}`))
	if w.Header().Get("x-atlas-effort") != "medium" || !strings.Contains(w.Body.String(), "reasoning_tokens") {
		t.Fatalf("effort: %v %s", w.Header(), w.Body.String())
	}
	w = f.do("POST", "/v1/chat/completions", f.key, econBody("chat-econ", "짧은 질문", ""))
	if w.Header().Get("x-atlas-econ-route") != "short-no-tools" || w.Header().Get("x-atlas-route") != "mock/mock-chat-small" || w.Header().Get("x-atlas-fallback") != "" {
		t.Fatalf("rule route: %v", w.Header())
	}
	w = f.do("POST", "/v1/chat/completions", f.key, econBody("chat-econ", "짧은 질문", ""), "x-atlas-route-pin", "true")
	if w.Header().Get("x-atlas-route") != "mock/mock-chat" {
		t.Fatalf("pin: %v", w.Header())
	}
	c := f.do("GET", "/admin/econ/models", admin, "")
	if c.Code != 200 || !strings.Contains(c.Body.String(), `"deprecation_in_days"`) || !strings.Contains(c.Body.String(), `"task_policies"`) {
		t.Fatalf("catalog: %d %s", c.Code, c.Body.String()[:200])
	}
}

func TestSimulateEndpoint(t *testing.T) {
	f := econSetup(t)
	w := f.do("POST", "/v1/econ/simulate", "", `{"model":"mock-chat-large","cache_ratio":0.6,"batch_ratio":0.3,"route_ratio":0.4,"route_model":"mock-chat-small","alt_model":"mock-chat"}`)
	var out econ.SimOutput
	if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &out) != nil || len(out.Waterfall) != 5 || out.TotalSavingPct <= 0 {
		t.Fatalf("simulate: %d %s", w.Code, w.Body.String())
	}
	if w := f.do("POST", "/v1/econ/simulate", "", `{"model":"nope"}`); w.Code != 400 {
		t.Fatalf("bad model: %d", w.Code)
	}
}

func TestBatchLifecycle(t *testing.T) {
	f := econSetup(t)
	jsonl := `{"custom_id":"a","method":"POST","url":"/v1/chat/completions","body":{"model":"chat-default","messages":[{"role":"user","content":"요약 1"}]}}
{"custom_id":"b","method":"POST","url":"/v1/chat/completions","body":{"model":"chat-default","messages":[{"role":"user","content":"요약 2 batch-fail"}]}}`
	w := f.do("POST", "/v1/batches", f.key, jsonl, "Content-Type", "application/jsonl")
	var b econ.Batch
	if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &b) != nil || b.Status != "in_progress" || b.Total != 2 {
		t.Fatalf("create: %d %s", w.Code, w.Body.String())
	}
	if o := f.do("GET", "/v1/batches/"+b.ID+"/output", f.key, ""); o.Code != 409 {
		t.Fatalf("output before end: %d", o.Code)
	}
	time.Sleep(5 * time.Millisecond)
	f.s.Batches.Tick(t.Context())
	g := f.do("GET", "/v1/batches/"+b.ID, f.key, "")
	_ = json.Unmarshal(g.Body.Bytes(), &b)
	if b.Status != "ended" || b.Completed != 1 || b.Failed != 1 || b.SavingsUSD == "0.00000000" {
		t.Fatalf("ended: %s", g.Body.String())
	}
	out := f.do("GET", "/v1/batches/"+b.ID+"/output", f.key, "").Body.String()
	if !strings.Contains(out, `"custom_id":"a"`) || !strings.Contains(out, `"status_code":200`) || !strings.Contains(out, `"custom_id":"b"`) {
		t.Fatalf("output: %s", out)
	}
	r := f.do("POST", "/v1/batches/"+b.ID+"/retry", f.key, "")
	if r.Code != 200 || !strings.Contains(r.Body.String(), `"round":1`) {
		t.Fatalf("retry: %d %s", r.Code, r.Body.String())
	}
	if !strings.Contains(subjects(f.rec), "gw.batch.completed") {
		t.Fatalf("event: %s", subjects(f.rec))
	}
	// 다른 tenant 는 볼 수 없음
	other := "sk-atlas-test-otherotherotherother00"
	_ = f.s.Keys.Put(t.Context(), other, auth.VirtualKey{Tenant: "beta", Models: []string{"*"}})
	if x := f.do("GET", "/v1/batches/"+b.ID, other, ""); x.Code != 404 {
		t.Fatalf("cross-tenant batch read: %d", x.Code)
	}
	bad := f.do("POST", "/v1/batches", f.key, `{"custom_id":"a","method":"POST","url":"/v1/embeddings","body":{"model":"chat-default","messages":[{"role":"user","content":"x"}]}}`, "Content-Type", "application/jsonl")
	if bad.Code != 400 {
		t.Fatalf("bad url accepted: %d", bad.Code)
	}
	mixed := f.do("POST", "/v1/batches", f.key, `{"requests":[{"custom_id":"a","method":"POST","url":"/v1/chat/completions","body":{"model":"chat-default","messages":[{"role":"user","content":"x"}]}},{"custom_id":"b","method":"POST","url":"/v1/chat/completions","body":{"model":"chat-small","messages":[{"role":"user","content":"y"}]}}]}`, "Content-Type", "application/json")
	if mixed.Code != 400 {
		t.Fatalf("mixed alias accepted: %d", mixed.Code)
	}
}
