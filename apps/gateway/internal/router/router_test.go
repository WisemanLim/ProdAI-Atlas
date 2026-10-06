package router

import (
	"context"
	"errors"
	"testing"
	"time"

	"atlas-gateway/internal/config"
	"atlas-gateway/internal/provider"
)

type counting struct {
	calls map[string]int
	fail  map[string]error
}

func (c *counting) Name() string { return "fake" }
func (c *counting) Complete(_ context.Context, r provider.Request) (provider.Response, error) {
	c.calls[r.Model]++
	if err := c.fail[r.Model]; err != nil {
		return provider.Response{}, err
	}
	return provider.Response{Content: "ok:" + r.Model}, nil
}

func newRouter(fail map[string]error) (*Router, *counting) {
	cfg := &config.Config{
		Aliases: map[string]config.Alias{
			"primary": {Deployments: []config.Deployment{{Provider: "fake", Model: "p"}}, Fallbacks: []string{"backup"}},
			"backup":  {Deployments: []config.Deployment{{Provider: "fake", Model: "b"}}},
		},
		Retry:   config.Retry{MaxAttempts: 3, BaseMS: 1, MaxMS: 2},
		Circuit: config.Circuit{WindowSec: 60, MinRequests: 3, FailureRatio: 0.5, CooldownSec: 30},
	}
	p := &counting{calls: map[string]int{}, fail: fail}
	r := New(cfg, map[string]provider.Provider{"fake": p})
	r.sleep = func(context.Context, time.Duration) error { return nil }
	return r, p
}

func TestFallbackAfterRetries(t *testing.T) {
	r, p := newRouter(map[string]error{"p": &provider.Error{Status: 503}})
	res, err := r.Route(context.Background(), "primary", provider.Request{})
	if err != nil || res.Alias != "backup" || res.Response.Content != "ok:b" {
		t.Fatalf("res=%+v err=%v", res, err)
	}
	if p.calls["p"] != 3 || len(res.Attempts) != 4 {
		t.Fatalf("calls=%v attempts=%v", p.calls, res.Attempts)
	}
}

func TestNonRetriableErrorDoesNotFallback(t *testing.T) {
	r, p := newRouter(map[string]error{"p": &provider.Error{Status: 400}})
	_, err := r.Route(context.Background(), "primary", provider.Request{})
	var pe *provider.Error
	if !errors.As(err, &pe) || pe.Status != 400 || p.calls["p"] != 1 || p.calls["b"] != 0 {
		t.Fatalf("err=%v calls=%v", err, p.calls)
	}
}

func TestCircuitOpensThenHalfOpens(t *testing.T) {
	r, p := newRouter(map[string]error{"p": &provider.Error{Status: 500}})
	now := time.Now()
	r.now = func() time.Time { return now }
	r.Route(context.Background(), "primary", provider.Request{}) // 3 실패 → open
	if r.CircuitStates()["fake/p"] != "open" {
		t.Fatalf("states=%v", r.CircuitStates())
	}
	before := p.calls["p"]
	res, _ := r.Route(context.Background(), "primary", provider.Request{})
	if p.calls["p"] != before || res.Alias != "backup" {
		t.Fatal("open circuit should skip primary")
	}
	now = now.Add(31 * time.Second)
	delete(p.fail, "p")
	res, _ = r.Route(context.Background(), "primary", provider.Request{})
	if res.Alias != "primary" || r.CircuitStates()["fake/p"] != "closed" {
		t.Fatalf("half-open success should close: alias=%s states=%v", res.Alias, r.CircuitStates())
	}
}

func TestUnknownAlias(t *testing.T) {
	r, _ := newRouter(nil)
	if _, err := r.Route(context.Background(), "nope", provider.Request{}); !errors.Is(err, ErrUnknownAlias) {
		t.Fatal(err)
	}
}
