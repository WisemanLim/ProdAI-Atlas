package econ

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"

	"atlas-gateway/internal/config"
	"atlas-gateway/internal/provider"
)

func cfg(t *testing.T) *config.Config {
	t.Helper()
	c, err := config.Load("")
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func ip(n int) *int { return &n }

func TestPlanRuleRoutingAndPin(t *testing.T) {
	c := cfg(t)
	p := Plan(c, PlanInput{Alias: "chat-econ", InputTokens: 100})
	if p.Alias != "chat-small" || p.Rule != "short-no-tools" {
		t.Fatalf("short request should route to chat-small: %+v", p)
	}
	if p := Plan(c, PlanInput{Alias: "chat-econ", InputTokens: 900}); p.Alias != "chat-econ" {
		t.Fatalf("long request must stay: %s", p.Alias)
	}
	if p := Plan(c, PlanInput{Alias: "chat-econ", InputTokens: 100, HasTools: true}); p.Alias != "chat-econ" {
		t.Fatalf("tools request must stay: %s", p.Alias)
	}
	if p := Plan(c, PlanInput{Alias: "chat-econ", InputTokens: 100, Pinned: true}); p.Alias != "chat-econ" {
		t.Fatalf("pinned must stay: %s", p.Alias)
	}
	if p := Plan(c, PlanInput{Alias: "chat-econ", InputTokens: 100, Allows: func(a string) bool { return a == "chat-econ" }}); p.Alias != "chat-econ" {
		t.Fatalf("disallowed target must not be used: %s", p.Alias)
	}
	if p := Plan(c, PlanInput{Alias: "chat-large", Tags: map[string]string{"feature": "classify"}}); p.Alias != "chat-small" || p.Rule != "tag-classify" {
		t.Fatalf("tag rule: %+v", p)
	}
}

func TestPlanModelPolicyAndMaxTokens(t *testing.T) {
	c := cfg(t)
	p := Plan(c, PlanInput{Alias: "chat-large", TaskType: "classify", MaxTokens: ip(500)})
	if p.Alias != "chat-small" || p.MaxTokens != 64 {
		t.Fatalf("classify caps tier and max_tokens: %+v", p)
	}
	if !strings.Contains(strings.Join(p.Mutations, ";"), "model_policy:classify:chat-large->chat-small") {
		t.Fatalf("mutation missing: %v", p.Mutations)
	}
	if p := Plan(c, PlanInput{Alias: "auto", TaskType: "reasoning"}); p.Alias != "reasoner" {
		t.Fatalf("auto → default alias: %s", p.Alias)
	}
	if p := Plan(c, PlanInput{Alias: "chat-small"}); p.MaxTokens != 256 || p.Mutations[0] != "max_tokens:default=256" {
		t.Fatalf("default injection: %+v", p)
	}
	if p := Plan(c, PlanInput{Alias: "chat-default", MaxTokens: ip(99999)}); p.MaxTokens != 4096 {
		t.Fatalf("global cap: %d", p.MaxTokens)
	}
}

func TestPlanEffort(t *testing.T) {
	c := cfg(t)
	if p := Plan(c, PlanInput{Alias: "reasoner", TaskType: "code", Effort: "high"}); p.Effort != "medium" || p.ThinkingBudget != 4096 {
		t.Fatalf("effort capped by task: %+v", p)
	}
	if p := Plan(c, PlanInput{Alias: "reasoner"}); p.Effort != "low" {
		t.Fatalf("reasoning default low: %+v", p)
	}
	if p := Plan(c, PlanInput{Alias: "chat-default", Effort: "high"}); p.Effort != "" || p.ThinkingBudget != 0 {
		t.Fatalf("non-reasoning drops effort: %+v", p)
	}
	if p := Plan(c, PlanInput{Alias: "reasoner", TaskType: "reasoning", Effort: "high"}); p.ThinkingBudget != 16384 {
		t.Fatalf("high budget: %+v", p)
	}
}

func TestCatalogDeprecationWarning(t *testing.T) {
	c := cfg(t)
	now := time.Date(2026, 10, 2, 0, 0, 0, 0, time.UTC)
	for _, e := range Catalog(c, now, func(string) map[string]string { return nil }) {
		if e.Model == "mock-chat-large" && (e.DeprecationIn == nil || *e.DeprecationIn != 44 || e.Warning == "") {
			t.Fatalf("deprecation warning: %+v", e)
		}
		if e.Model == "mock-chat" && e.Warning != "" {
			t.Fatalf("unexpected warning: %+v", e)
		}
	}
}

func rdb(t *testing.T) (*redis.Client, *miniredis.Miniredis) {
	mr := miniredis.RunT(t)
	return redis.NewClient(&redis.Options{Addr: mr.Addr()}), mr
}

func TestBudgetLifecycle(t *testing.T) {
	r, _ := rdb(t)
	ctx := context.Background()
	now := time.Date(2026, 10, 2, 12, 0, 0, 0, time.UTC)
	var events []map[string]any
	b := &Budgets{R: r, Now: func() time.Time { return now }, SoftPct: 80, Publish: func(_ context.Context, s string, d any) {
		events = append(events, d.(map[string]any))
	}}
	x := Budget{ID: "acme-day", Scope: "org", ScopeID: "acme", AmountUSD: "1.00", Period: "daily"}
	if err := b.Validate(&x, func(string) bool { return true }); err != nil || x.HardAction != "block" || x.SoftPct != 80 {
		t.Fatalf("validate defaults: %v %+v", err, x)
	}
	if err := b.Validate(&Budget{ID: "x", Scope: "team", ScopeID: "a", AmountUSD: "1", Period: "daily"}, nil); err == nil {
		t.Fatal("bad scope accepted")
	}
	_ = b.Put(ctx, x)
	sc := Scopes{Org: "acme", Key: "k1"}
	b.Settle(ctx, sc, 85_000_000) // $0.85 — 50·80% 임계 이벤트 2개 + 상태 변화 1개
	v, _ := b.Check(ctx, sc)
	var th []int
	for _, e := range events {
		if x, ok := e["threshold"].(int); ok {
			th = append(th, x)
		}
	}
	events = filterState(events)
	if len(th) != 2 || th[0] != 50 || th[1] != 80 {
		t.Fatalf("thresholds: %v", th)
	}
	if v.Action != "allow" || len(v.Warnings) != 1 || len(events) != 1 || events[0]["to"] != "warning" {
		t.Fatalf("warning state: %+v events=%v", v, events)
	}
	b.Settle(ctx, sc, 20_000_000)
	events = filterState(events)
	if v, _ := b.Check(ctx, sc); v.Action != "block" || events[len(events)-1]["to"] != "exceeded" {
		t.Fatalf("block state: %+v %v", v, events)
	}
	if v, _ := b.Check(ctx, Scopes{Org: "other"}); v.Action != "allow" {
		t.Fatal("other tenant affected")
	}
	now = now.Add(24 * time.Hour) // 다음 날 리셋
	if v, _ := b.Check(ctx, sc); v.Action != "allow" || len(v.Warnings) != 0 {
		t.Fatalf("daily reset: %+v", v)
	}
}

func TestBudgetWindows(t *testing.T) {
	at := time.Date(2026, 10, 2, 16, 0, 0, 0, time.UTC) // KST 10-03 01:00, 금요일
	if w := window(Budget{Period: "daily", TZ: "Asia/Seoul"}, at); w != "2026-10-03" {
		t.Fatalf("tz daily: %s", w)
	}
	if w := window(Budget{Period: "weekly", TZ: "UTC"}, at); w != "2026-09-28" {
		t.Fatalf("weekly monday: %s", w)
	}
	if w := window(Budget{Period: "monthly", TZ: "UTC"}, at); w != "2026-10-01" {
		t.Fatalf("monthly: %s", w)
	}
}

func TestTaskBudget(t *testing.T) {
	r, _ := rdb(t)
	ctx := context.Background()
	n := 0
	tk := &Tasks{R: r, Default: 1000, SoftPct: 80, Publish: func(context.Context, string, any) { n++ }}
	if tk.Limit("500") != 500 || tk.Limit("5000") != 1000 || tk.Limit("") != 1000 {
		t.Fatal("limit header may only lower the default")
	}
	tk.Add(ctx, "t1", 700, 1000, "acme")
	tk.Add(ctx, "t1", 200, 1000, "acme")
	tk.Add(ctx, "t1", 50, 1000, "acme")
	if tk.Used(ctx, "t1") != 950 || n != 1 {
		t.Fatalf("used=%d near-limit events=%d", tk.Used(ctx, "t1"), n)
	}
}

func TestExactKeyAndEligibility(t *testing.T) {
	m := []provider.Message{{Role: "user", Content: "hi"}}
	g0 := map[string]any{"temperature": 0.0}
	if ExactKey("a", "x", "", m, g0) != ExactKey("a", "x", "", m, map[string]any{"temperature": 0.0}) {
		t.Fatal("same request, different key")
	}
	if ExactKey("a", "x", "", m, g0) == ExactKey("b", "x", "", m, g0) || ExactKey("a", "x", "low", m, g0) == ExactKey("a", "x", "high", m, g0) {
		t.Fatal("tenant must partition the cache")
	}
	if ok, _ := ExactEligible(true, g0, ""); !ok {
		t.Fatal("temperature 0 eligible")
	}
	if ok, st := ExactEligible(true, map[string]any{}, ""); ok || st != "SKIP-TEMPERATURE" {
		t.Fatal("missing temperature must skip")
	}
	if ok, st := ExactEligible(true, g0, "no-cache"); ok || st != "BYPASS" {
		t.Fatal("no-cache bypass")
	}
}

func TestPromptCacheKeyAndTTL(t *testing.T) {
	long := strings.Repeat("정책 문서 ", 1200)
	msgs := []provider.Message{{Role: "system", Content: long}, {Role: "user", Content: "q1"}}
	k := PromptCacheKey("acme", "chat-default", msgs, 0, 1024)
	if k == "" || k != PromptCacheKey("acme", "chat-default", []provider.Message{{Role: "system", Content: long}, {Role: "user", Content: "q2"}}, 0, 1024) {
		t.Fatal("same system prompt → same key regardless of user turn")
	}
	if PromptCacheKey("acme", "chat-default", []provider.Message{{Role: "system", Content: "short"}, {Role: "user", Content: "q"}}, 0, 1024) != "" {
		t.Fatal("short prefix must not get a cache key")
	}
	r, _ := rdb(t)
	now := time.Now()
	if CacheTTL(context.Background(), r, k, now) != "5m" || CacheTTL(context.Background(), r, k, now.Add(time.Minute)) != "5m" {
		t.Fatal("frequent reuse → 5m")
	}
	if CacheTTL(context.Background(), r, k, now.Add(20*time.Minute)) != "1h" {
		t.Fatal("reuse gap > 5m → 1h")
	}
}

func TestSimulateWaterfall(t *testing.T) {
	prices := func(m string) (float64, float64, float64, float64, bool) {
		switch m {
		case "big":
			return 3, 15, 0.3, 3.75, true
		case "small":
			return 0.05, 0.2, 0.005, 0.0625, true
		}
		return 0, 0, 0, 0, false
	}
	out, err := Simulate(SimInput{TokensPerTask: 50000, InputRatio: 0.8, CacheRatio: 0.6, BatchRatio: 0.3, RouteRatio: 0.4, RouteModel: "small",
		TasksPerDay: 1000, Model: "big", AltModel: "small"}, prices, 0.5, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	// baseline = (40000×3 + 10000×15)/1e6 = 0.27
	if out.Waterfall[0].PerTaskUSD != 0.27 || len(out.Waterfall) != 5 {
		t.Fatalf("baseline: %+v", out.Waterfall)
	}
	for i := 1; i < len(out.Waterfall); i++ {
		if out.Waterfall[i].PerTaskUSD >= out.Waterfall[i-1].PerTaskUSD || out.Waterfall[i].SavingsUSD <= 0 {
			t.Fatalf("each lever must save: %+v", out.Waterfall[i])
		}
	}
	if out.TotalSavingPct <= 50 || len(out.Sensitivity["cache_ratio"]) != 5 {
		t.Fatalf("total/sensitivity: %+v", out)
	}
	// 이차 증가 모델: N=10, S=1000, d=500 → 10000 + 500×45 = 32500
	turns, _ := Simulate(SimInput{Turns: &struct {
		N int `json:"n"`
		S int `json:"s"`
		D int `json:"d"`
		O int `json:"o"`
	}{10, 1000, 500, 200}, Model: "big"}, prices, 0.5, time.Now())
	if turns.InputTokens != 32500 || turns.OutputTokens != 2000 {
		t.Fatalf("quadratic: %v %v", turns.InputTokens, turns.OutputTokens)
	}
	if _, err := Simulate(SimInput{Model: "nope"}, prices, 0.5, time.Now()); err == nil {
		t.Fatal("unknown model accepted")
	}
}

func TestBudgetSeedsFromPriorSpend(t *testing.T) {
	r, _ := rdb(t)
	ctx := context.Background()
	now := time.Date(2026, 10, 2, 12, 0, 0, 0, time.UTC)
	b := &Budgets{R: r, Now: func() time.Time { return now }, SoftPct: 80}
	sc := Scopes{Org: "acme", Project: "p1", Key: "k1"}
	b.Settle(ctx, sc, 50_000_000) // 예산 만들기 전 $0.50 지출
	now = now.Add(2 * time.Hour)
	x := Budget{ID: "late", Scope: "project", ScopeID: "p1", AmountUSD: "0.40", Period: "daily", TZ: "UTC", SoftPct: 80, HardAction: "block"}
	_ = b.Put(ctx, x)
	if v, _ := b.Check(ctx, sc); v.Action != "block" {
		t.Fatalf("budget created mid-period must see prior spend: %+v", v)
	}
	y := Budget{ID: "yday", Scope: "org", ScopeID: "acme", AmountUSD: "0.40", Period: "daily", TZ: "UTC", SoftPct: 80, HardAction: "block"}
	now = now.Add(24 * time.Hour)
	_ = b.Put(ctx, y)
	if v, _ := b.Check(ctx, Scopes{Org: "acme"}); v.Action != "allow" {
		t.Fatalf("yesterday's spend must not count: %+v", v)
	}
}

func filterState(evs []map[string]any) []map[string]any {
	var out []map[string]any
	for _, e := range evs {
		if _, ok := e["to"]; ok {
			out = append(out, e)
		}
	}
	return out
}

// 예산 상속 (FR-PLT-TEN-04): Manager 가 내려 준 org/proj · org/proj/env 범위가 가상키 project·metadata.env 로 집행된다.
func TestBudgetQualifiedProjectAndEnvScopes(t *testing.T) {
	r, _ := rdb(t)
	ctx := context.Background()
	now := time.Date(2026, 10, 2, 12, 0, 0, 0, time.UTC)
	b := &Budgets{R: r, Now: func() time.Time { return now }, SoftPct: 80}
	for _, x := range []Budget{{ID: "p", Scope: "project", ScopeID: "acme/default", AmountUSD: "10", Period: "monthly"},
		{ID: "e", Scope: "env", ScopeID: "acme/default/prod", AmountUSD: "1", Period: "monthly"}} {
		if err := b.Validate(&x, func(string) bool { return true }); err != nil {
			t.Fatal(err)
		}
		_ = b.Put(ctx, x)
	}
	prod := Scopes{Org: "acme", Project: "default", Env: "prod", Key: "k"}
	b.Settle(ctx, prod, 150_000_000) // $1.50 → env 초과 · project 15%
	if v, _ := b.Check(ctx, prod); v.Action != "block" || v.Budget != "e" {
		t.Fatalf("env budget: %+v", v)
	}
	if v, _ := b.Check(ctx, Scopes{Org: "acme", Project: "default", Env: "dev", Key: "k"}); v.Action != "allow" {
		t.Fatalf("other env: %+v", v)
	}
	if v, _ := b.Check(ctx, Scopes{Org: "beta", Project: "default", Key: "k"}); v.Action != "allow" || len(v.Warnings) != 0 {
		t.Fatalf("other org same project id: %+v", v)
	}
}
