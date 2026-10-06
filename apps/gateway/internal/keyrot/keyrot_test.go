package keyrot

import (
	"context"
	"errors"
	"testing"
	"time"

	"atlas-gateway/internal/provider"
	"atlas-gateway/internal/secrets"
)

func setup(t *testing.T) (*Manager, *provider.KeyPool, *time.Time, *[]string) {
	st, _ := secrets.Open("file://" + t.TempDir())
	pool := provider.NewKeyPool("openai", []string{"env-key-1"})
	now := time.Date(2026, 10, 2, 0, 0, 0, 0, time.UTC)
	var events []string
	m := &Manager{Store: st, Pools: map[string]*provider.KeyPool{"openai": pool}, Now: func() time.Time { return now },
		Validate: func(_ context.Context, _, key string) error {
			if key == "bad" {
				return errors.New("401")
			}
			return nil
		},
		Publish: func(_ context.Context, s string, _ any) { events = append(events, s) }}
	return m, pool, &now, &events
}

func pick(p *provider.KeyPool) string { k, _, _ := p.Pick(); return k }

func TestImportRotateGraceRetire(t *testing.T) {
	m, pool, now, events := setup(t)
	ctx := context.Background()
	if err := m.Load(ctx); err != nil {
		t.Fatal(err)
	}
	st, _ := m.Status(ctx, "openai")
	if st.Version != 1 || st.ActiveKeys != 1 {
		t.Fatalf("env import: %+v", st)
	}
	if _, err := m.Rotate(ctx, "openai", "bad"); !errors.Is(err, ErrInvalidKey) || pick(pool) != "env-key-1" {
		t.Fatalf("invalid key must not replace: %v", err)
	}
	st, err := m.Rotate(ctx, "openai", "new-key-2")
	if err != nil || st.RetiringKeys != 1 || st.ActiveKeys != 1 {
		t.Fatalf("rotate: %+v %v", st, err)
	}
	if pick(pool) != "new-key-2" {
		t.Fatal("new key must be primary")
	}
	pool.Compromise(0) // 새 키 장애 시 유예 중 이전 키로 계속
	if pick(pool) != "env-key-1" {
		t.Fatal("retiring key must serve during grace when no active key")
	}
	*now = now.Add(25 * time.Hour)
	m.Sync(ctx)
	if got := pool.Secrets("retiring"); len(got) != 0 {
		t.Fatalf("retiring after grace: %v", got)
	}
	if last := (*events)[len(*events)-1]; last != "gw.key.retired" {
		t.Fatalf("events: %v", *events)
	}
}

func TestRotationDueWarningAndExternalChange(t *testing.T) {
	m, pool, now, events := setup(t)
	ctx := context.Background()
	_ = m.Load(ctx)
	*now = now.Add(80 * 24 * time.Hour)
	m.Sync(ctx)
	m.Sync(ctx)
	n := 0
	for _, e := range *events {
		if e == "gw.key.rotation_due" {
			n++
		}
	}
	if n != 1 {
		t.Fatalf("due warning once: %v", *events)
	}
	// 다른 인스턴스가 회전 → Sync 가 반영
	other := &Manager{Store: m.Store, Pools: map[string]*provider.KeyPool{"openai": provider.NewKeyPool("openai", nil)}, Now: m.Now}
	_, _ = other.Rotate(ctx, "openai", "from-other")
	m.Sync(ctx)
	if pick(pool) != "from-other" {
		t.Fatal("external rotation not applied")
	}
}
