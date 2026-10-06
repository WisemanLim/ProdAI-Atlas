// Package keyrot 는 공급자 API 키 무중단 회전이다 (FR-PLT-SEC-04 · GW-KEY):
// 비밀 저장소 providers/{provider} = {"keys":[현재], "previous":[이전], "previous_until", "rotated_at"} 를 정본으로,
// 새 키 검증 호출 성공 → current 교체(이전 키는 유예 24h 동안 retiring) → 유예 후 폐기. 회전 주기 90일, 14일 전 알림.
package keyrot

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"atlas-gateway/internal/provider"
	"atlas-gateway/internal/secrets"
)

type Slots struct {
	Keys          []string  `json:"keys"`
	Previous      []string  `json:"previous,omitempty"`
	PreviousUntil time.Time `json:"previous_until,omitempty"`
	RotatedAt     time.Time `json:"rotated_at"`
}

type Status struct {
	Provider      string     `json:"provider"`
	Version       int        `json:"version"`
	ActiveKeys    int        `json:"activeKeys"`
	RetiringKeys  int        `json:"retiringKeys"`
	RetiringUntil *time.Time `json:"retiringUntil,omitempty"`
	RotatedAt     time.Time  `json:"rotatedAt"`
	DueAt         time.Time  `json:"dueAt"`
	Warning       string     `json:"warning,omitempty"`
	Backend       string     `json:"backend"`
}

type Manager struct {
	Store        secrets.Store
	Pools        map[string]*provider.KeyPool
	Validate     func(ctx context.Context, provider, key string) error
	Publish      func(ctx context.Context, subject string, data any)
	Now          func() time.Time
	Grace        time.Duration // 기본 24h
	RotationDays int           // 기본 90
	mu           sync.Mutex
	versions     map[string]int
	dueWarned    map[string]string
}

var ErrInvalidKey = errors.New("new key failed validation")

func path(p string) string { return "providers/" + p }

func (m *Manager) init() {
	if m.versions == nil {
		m.versions, m.dueWarned = map[string]int{}, map[string]string{}
	}
	if m.Grace == 0 {
		m.Grace = 24 * time.Hour
	}
	if m.RotationDays == 0 {
		m.RotationDays = 90
	}
	if m.Now == nil {
		m.Now = time.Now
	}
}

func (m *Manager) read(ctx context.Context, p string) (Slots, int, error) {
	s, err := m.Store.Get(ctx, path(p))
	if err != nil {
		return Slots{}, 0, err
	}
	var sl Slots
	if err := json.Unmarshal([]byte(s.Value), &sl); err != nil {
		return Slots{}, 0, fmt.Errorf("providers/%s: 형식 오류", p)
	}
	return sl, s.Version, nil
}

func (m *Manager) apply(p string, sl Slots, version int) {
	var retiring []string
	if m.Now().Before(sl.PreviousUntil) {
		retiring = sl.Previous
	}
	m.Pools[p].Replace(sl.Keys, retiring)
	m.versions[p] = version
}

// Load 는 기동 시 저장소의 키로 풀을 채운다. 저장소에 없고 env 키가 있으면 저장소로 이관한다(정본 = 저장소).
func (m *Manager) Load(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.init()
	for p, pool := range m.Pools {
		sl, v, err := m.read(ctx, p)
		if errors.Is(err, secrets.ErrNotFound) {
			env := pool.Secrets("active")
			if len(env) == 0 {
				continue
			}
			b, _ := json.Marshal(Slots{Keys: env, RotatedAt: m.Now().UTC()})
			meta, err := m.Store.Put(ctx, path(p), string(b))
			if err != nil {
				return err
			}
			m.versions[p] = meta.Version
			slog.Info("provider keys imported to secret store", "provider", p, "keys", len(env))
			continue
		}
		if err != nil {
			return err
		}
		m.apply(p, sl, v)
		slog.Info("provider keys loaded from secret store", "provider", p, "version", v, "keys", len(sl.Keys))
	}
	return nil
}

// Rotate 는 새 키를 검증하고 current 로 올린다. 이전 키는 Grace 동안 retiring.
func (m *Manager) Rotate(ctx context.Context, p, newKey string) (Status, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.init()
	if _, ok := m.Pools[p]; !ok {
		return Status{}, fmt.Errorf("provider %s 없음", p)
	}
	if strings.TrimSpace(newKey) == "" {
		return Status{}, ErrInvalidKey
	}
	if m.Validate != nil {
		if err := m.Validate(ctx, p, newKey); err != nil {
			return Status{}, fmt.Errorf("%w: %v", ErrInvalidKey, err)
		}
	}
	cur, _, err := m.read(ctx, p)
	if err != nil && !errors.Is(err, secrets.ErrNotFound) {
		return Status{}, err
	}
	if len(cur.Keys) == 0 {
		cur.Keys = m.Pools[p].Secrets("active")
	}
	now := m.Now().UTC()
	next := Slots{Keys: []string{newKey}, Previous: cur.Keys, PreviousUntil: now.Add(m.Grace), RotatedAt: now}
	b, _ := json.Marshal(next)
	_, meta, err := m.Store.Rotate(ctx, path(p), string(b))
	if err != nil {
		return Status{}, err
	}
	m.apply(p, next, meta.Version)
	delete(m.dueWarned, p)
	if m.Publish != nil {
		m.Publish(ctx, "gw.key.rotated", map[string]any{"provider": p, "version": meta.Version, "retiring_until": next.PreviousUntil, "retiring": len(next.Previous)})
	}
	return m.status(p, next, meta.Version), nil
}

func (m *Manager) status(p string, sl Slots, v int) Status {
	due := sl.RotatedAt.Add(time.Duration(m.RotationDays) * 24 * time.Hour)
	st := Status{Provider: p, Version: v, ActiveKeys: len(sl.Keys), RotatedAt: sl.RotatedAt, DueAt: due, Backend: m.Store.Kind()}
	if m.Now().Before(sl.PreviousUntil) {
		st.RetiringKeys = len(sl.Previous)
		u := sl.PreviousUntil
		st.RetiringUntil = &u
	}
	switch left := due.Sub(m.Now()); {
	case left <= 0:
		st.Warning = "회전 기한 지남"
	case left <= 14*24*time.Hour:
		st.Warning = fmt.Sprintf("회전 기한 %d일 전", int(left.Hours()/24))
	}
	return st
}

func (m *Manager) Status(ctx context.Context, p string) (Status, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.init()
	sl, v, err := m.read(ctx, p)
	if err != nil {
		return Status{}, err
	}
	return m.status(p, sl, v), nil
}

// Sync 는 저장소 버전 변화(다른 인스턴스·운영자 회전)를 반영하고, 유예가 끝난 이전 키를 폐기하며, 회전 기한 알림을 낸다.
func (m *Manager) Sync(ctx context.Context) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.init()
	for p := range m.Pools {
		sl, v, err := m.read(ctx, p)
		if err != nil {
			continue
		}
		if len(sl.Previous) > 0 && !m.Now().Before(sl.PreviousUntil) { // 유예 종료 → 구 키 폐기
			retired := len(sl.Previous)
			sl.Previous, sl.PreviousUntil = nil, time.Time{}
			b, _ := json.Marshal(sl)
			if meta, err := m.Store.Put(ctx, path(p), string(b)); err == nil {
				v = meta.Version
				if m.Publish != nil {
					m.Publish(ctx, "gw.key.retired", map[string]any{"provider": p, "retired": retired, "version": v})
				}
			}
		}
		if v != m.versions[p] || len(sl.Previous) == 0 {
			m.apply(p, sl, v)
		}
		if st := m.status(p, sl, v); st.Warning != "" && m.dueWarned[p] != st.DueAt.Format("2006-01-02") && m.Publish != nil {
			m.dueWarned[p] = st.DueAt.Format("2006-01-02")
			m.Publish(ctx, "gw.key.rotation_due", map[string]any{"provider": p, "due_at": st.DueAt, "warning": st.Warning})
		}
	}
}

func (m *Manager) Run(ctx context.Context, every time.Duration) {
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			m.Sync(ctx)
		}
	}
}
