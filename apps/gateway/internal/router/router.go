// Package router 는 alias → 배포 선택 · 재시도(지수 백오프+full jitter) · 서킷브레이커 · 폴백 체인이다
// (FR-GW-ROUTE-03·05·07). M0: 서킷 상태는 인스턴스 메모리(Redis 공유는 M1).
package router

import (
	"context"
	"errors"
	"fmt"
	"hash/fnv"
	"math/rand/v2"
	"sync"
	"time"

	"atlas-gateway/internal/config"
	"atlas-gateway/internal/provider"
)

var ErrUnknownAlias = errors.New("model_not_found")

type Attempt struct {
	Alias    string `json:"alias"`
	Provider string `json:"provider"`
	Model    string `json:"model"`
	Error    string `json:"error,omitempty"`
}

type Result struct {
	Response provider.Response
	Alias    string // 실제 응답한 alias (폴백 시 원래와 다름)
	Provider string
	Model    string
	Attempts []Attempt
}

type Router struct {
	cfg       *config.Config
	providers map[string]provider.Provider
	sleep     func(context.Context, time.Duration) error
	mu        sync.Mutex
	circuits  map[string]*circuit
	now       func() time.Time
	ovMu      sync.RWMutex
	overlay   map[string]config.Alias // 런타임 alias 오버레이 (BL-MODEL-02·05) — 설정 파일 alias 를 덮거나 새로 만든다
}

// SetOverlay 는 런타임 alias 오버레이를 통째로 바꾼다 (aliases.Store 가 주기 동기화).
func (r *Router) SetOverlay(m map[string]config.Alias) {
	r.ovMu.Lock()
	r.overlay = m
	r.ovMu.Unlock()
}

// Alias 는 유효 alias 다: 오버레이 > 설정 파일.
func (r *Router) Alias(name string) (config.Alias, bool) {
	r.ovMu.RLock()
	a, ok := r.overlay[name]
	r.ovMu.RUnlock()
	if ok {
		return a, true
	}
	a, ok = r.cfg.Aliases[name]
	return a, ok
}

// Known 은 alias 존재 여부다.
func (r *Router) Known(name string) bool {
	_, ok := r.Alias(name)
	return ok
}

func New(cfg *config.Config, providers map[string]provider.Provider) *Router {
	for _, a := range cfg.Aliases { // 설정 파일 alias 의 weight 생략 = 1 (0 은 오버레이의 "트래픽 없음")
		for i := range a.Deployments {
			if a.Deployments[i].Weight <= 0 {
				a.Deployments[i].Weight = 1
			}
		}
	}
	return &Router{cfg: cfg, providers: providers, circuits: map[string]*circuit{}, now: time.Now,
		sleep: func(ctx context.Context, d time.Duration) error {
			select {
			case <-time.After(d):
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		}}
}

// Route 는 alias 를 시도하고 실패(재시도 가능 오류)하면 fallbacks 를 순서대로 시도한다.
func (r *Router) Route(ctx context.Context, alias string, req provider.Request) (Result, error) {
	al, ok := r.Alias(alias)
	if !ok {
		return Result{}, ErrUnknownAlias
	}
	var attempts []Attempt
	var lastErr error
	for _, a := range append([]string{alias}, al.Fallbacks...) {
		res, err := r.tryAlias(ctx, a, req, &attempts)
		if err == nil {
			res.Attempts = attempts
			return res, nil
		}
		lastErr = err
		if !provider.Retriable(err) && !errors.Is(err, errCircuitOpen) {
			break // 4xx 등은 폴백하지 않는다
		}
	}
	return Result{Attempts: attempts}, lastErr
}

var errCircuitOpen = errors.New("circuit open")

func (r *Router) tryAlias(ctx context.Context, alias string, req provider.Request, attempts *[]Attempt) (Result, error) {
	d, ok := r.pick(alias, req.AssignKey)
	if !ok {
		*attempts = append(*attempts, Attempt{Alias: alias, Error: errCircuitOpen.Error()})
		return Result{}, errCircuitOpen
	}
	p, ok := r.providers[d.Provider]
	if !ok {
		err := &provider.Error{Status: 503, Msg: "provider " + d.Provider + " 미설정"}
		*attempts = append(*attempts, Attempt{Alias: alias, Provider: d.Provider, Model: d.Model, Error: err.Error()})
		return Result{}, err
	}
	req.Model = d.Model
	var err error
	for i := 0; i < max(1, r.cfg.Retry.MaxAttempts); i++ {
		if i > 0 {
			if serr := r.sleep(ctx, r.backoff(i)); serr != nil {
				return Result{}, serr
			}
		}
		var resp provider.Response
		resp, err = p.Complete(ctx, req)
		r.record(d, err)
		if err == nil {
			*attempts = append(*attempts, Attempt{Alias: alias, Provider: d.Provider, Model: d.Model})
			return Result{Response: resp, Alias: alias, Provider: d.Provider, Model: d.Model}, nil
		}
		*attempts = append(*attempts, Attempt{Alias: alias, Provider: d.Provider, Model: d.Model, Error: err.Error()})
		if !provider.Retriable(err) || r.isOpen(d) {
			break
		}
	}
	return Result{}, err
}

// backoff = full jitter: rand(0, min(max, base·2^i))
func (r *Router) backoff(i int) time.Duration {
	ceil := min(r.cfg.Retry.MaxMS, r.cfg.Retry.BaseMS<<i)
	return time.Duration(rand.IntN(max(1, ceil))) * time.Millisecond
}

// pick 은 서킷이 닫힌(또는 half-open) 배포 중 가중치 랜덤으로 고른다.
// Resolve 는 alias 의 배포 하나와 공급자를 고른다 (배치 제출용 — 재시도·폴백 없이).
func (r *Router) Resolve(alias string) (provider.Provider, config.Deployment, bool) {
	d, ok := r.pick(alias, "")
	if !ok {
		return nil, d, false
	}
	p, ok := r.providers[d.Provider]
	return p, d, ok
}

// weight 0 = 트래픽 없음(후보 0% 등록, BL-MODEL-03). assignKey 가 있으면 같은 키는 같은 배포로 고정 배정 (BL-MODEL-05).
func (r *Router) pick(alias, assignKey string) (config.Deployment, bool) {
	a, _ := r.Alias(alias)
	var live []config.Deployment
	total := 0
	for _, d := range a.Deployments {
		if d.Weight > 0 && r.allow(d) {
			live = append(live, d)
			total += d.Weight
		}
	}
	if len(live) == 0 {
		return config.Deployment{}, false
	}
	n := rand.IntN(total)
	if assignKey != "" {
		h := fnv.New32a()
		_, _ = h.Write([]byte(alias + "\x00" + assignKey))
		n = int(h.Sum32() % uint32(total))
	}
	for _, d := range live {
		if n -= d.Weight; n < 0 {
			return d, true
		}
	}
	return live[len(live)-1], true
}

// --- 서킷브레이커: 슬라이딩 윈도 실패율 > ratio(최소 요청 수 이상) → open(cooldown) → half-open 1요청 ---

type circuit struct {
	events   []event
	openTill time.Time
	halfOpen bool
}

type event struct {
	at time.Time
	ok bool
}

func key(d config.Deployment) string { return fmt.Sprintf("%s/%s", d.Provider, d.Model) }

func (r *Router) c(d config.Deployment) *circuit {
	k := key(d)
	if r.circuits[k] == nil {
		r.circuits[k] = &circuit{}
	}
	return r.circuits[k]
}

func (r *Router) allow(d config.Deployment) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	c := r.c(d)
	if c.openTill.IsZero() {
		return true
	}
	if r.now().Before(c.openTill) {
		return false
	}
	if c.halfOpen {
		return false // half-open 시험 요청이 이미 진행 중
	}
	c.halfOpen = true
	return true
}

func (r *Router) isOpen(d config.Deployment) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return !r.c(d).openTill.IsZero()
}

func (r *Router) record(d config.Deployment, err error) {
	if err != nil && !provider.Retriable(err) {
		return // 클라이언트 오류는 배포 건강도와 무관
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	c, now := r.c(d), r.now()
	if c.halfOpen || !c.openTill.IsZero() && !now.Before(c.openTill) {
		c.halfOpen = false
		if err == nil {
			c.openTill, c.events = time.Time{}, nil
		} else {
			c.openTill = now.Add(time.Duration(r.cfg.Circuit.CooldownSec) * time.Second)
		}
		return
	}
	window := now.Add(-time.Duration(r.cfg.Circuit.WindowSec) * time.Second)
	kept := c.events[:0]
	for _, e := range c.events {
		if e.at.After(window) {
			kept = append(kept, e)
		}
	}
	c.events = append(kept, event{now, err == nil})
	fails := 0
	for _, e := range c.events {
		if !e.ok {
			fails++
		}
	}
	if len(c.events) >= r.cfg.Circuit.MinRequests && float64(fails)/float64(len(c.events)) > r.cfg.Circuit.FailureRatio {
		c.openTill = now.Add(time.Duration(r.cfg.Circuit.CooldownSec) * time.Second)
	}
}

// CircuitStates 는 관리 API 용 스냅숏이다.
func (r *Router) CircuitStates() map[string]string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := map[string]string{}
	for k, c := range r.circuits {
		switch {
		case c.openTill.IsZero():
			out[k] = "closed"
		case r.now().Before(c.openTill):
			out[k] = "open"
		default:
			out[k] = "half-open"
		}
	}
	return out
}
