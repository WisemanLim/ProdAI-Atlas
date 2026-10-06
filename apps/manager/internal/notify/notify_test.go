package notify

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"atlas-manager/internal/events"
	"atlas-manager/internal/store"
)

func TestTopicAndMatch(t *testing.T) {
	for _, c := range []struct {
		p, s string
		ok   bool
	}{{"gw.budget.threshold", "gw.budget.threshold", true}, {"gw.*.threshold", "gw.budget.threshold", true}, {"plt.>", "plt.approval.requested", true},
		{"plt.>", "plt", false}, {"gw.budget", "gw.budget.threshold", false}, {"gw.*", "gw.budget.threshold", false}} {
		if Topic(c.p, c.s) != c.ok {
			t.Errorf("Topic(%s,%s) != %v", c.p, c.s, c.ok)
		}
	}
	d := map[string]any{"threshold": 80.0, "scope": "org", "risk": "high", "meta": map[string]any{"name": "budget-a"}}
	if !Match([]Cond{{Field: "threshold", Op: ">=", Value: 80}, {Field: "risk", Op: "in", Value: []any{"high", "critical"}}, {Field: "meta.name", Op: "contains", Value: "budget"}}, d) {
		t.Fatal("match all")
	}
	if Match([]Cond{{Field: "threshold", Op: ">", Value: 80}}, d) || Match([]Cond{{Field: "missing", Op: "==", Value: 1}}, d) || Match([]Cond{{Field: "scope", Op: "~", Value: 1}}, d) {
		t.Fatal("should not match")
	}
	if OrgOf(map[string]any{"scope": "org", "scope_id": "acme"}) != "acme" || OrgOf(map[string]any{"org": "beta"}) != "beta" || OrgOf(nil) != "default" {
		t.Fatal("OrgOf")
	}
}

func TestCheckURLBlocksInternal(t *testing.T) {
	e := &Engine{AllowHosts: Hosts("hooks.local, 127.0.0.1")}
	for _, u := range []string{"http://169.254.169.254/latest", "https://127.0.0.2/x", "ftp://x", "http://example.com/hook", "https://localhost/x"} {
		if e.CheckURL(u) == nil {
			t.Errorf("%s allowed", u)
		}
	}
	for _, u := range []string{"http://hooks.local/x", "http://127.0.0.1:9/x"} {
		if err := e.CheckURL(u); err != nil {
			t.Errorf("%s: %v", u, err)
		}
	}
}

func TestHandleDeliversSignedWebhookWithThrottle(t *testing.T) {
	db, err := store.Open("sqlite", "file:"+filepath.Join(t.TempDir(), "n.db"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Migrate(db, "sqlite"); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var got []*http.Request
	var bodies []string
	fails := 1
	hook := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		defer mu.Unlock()
		if fails > 0 { // 첫 시도 5xx → 재시도
			fails--
			w.WriteHeader(503)
			return
		}
		got, bodies = append(got, r), append(bodies, string(b))
	}))
	defer hook.Close()
	u, _ := url.Parse(hook.URL)
	now := time.Unix(1_800_000_000, 0)
	e := &Engine{DB: db, HTTP: hook.Client(), AllowHosts: Hosts(u.Hostname()), Now: func() time.Time { return now }}
	db.Create(&store.NotificationRule{ID: "r1", OrgID: "acme", Name: "budget-80", Topic: "gw.budget.threshold", Filter: `[{"field":"threshold","op":">=","value":80}]`,
		Severity: "warning", Channels: `[{"type":"webhook","url":"` + hook.URL + `","secret":"s3cret"},{"type":"webhook","url":"http://169.254.169.254/x"}]`, ThrottleS: 60, Enabled: true})
	db.Create(&store.NotificationRule{ID: "r2", OrgID: "beta", Name: "other-org", Topic: "gw.>", Channels: `[{"type":"webhook","url":"` + hook.URL + `"}]`, Enabled: true})
	ev := func(th float64) events.Event {
		return events.Event{ID: "e", Type: "gw.budget.threshold", Time: now, Data: map[string]any{"org": "acme", "threshold": th, "pct": th}}
	}
	ctx := context.Background()
	e.Handle(ctx, "gw.budget.threshold", ev(50)) // 조건 불일치
	e.Handle(ctx, "gw.budget.threshold", ev(80))
	e.Handle(ctx, "gw.budget.threshold", ev(100)) // 스로틀
	if len(got) != 1 {
		t.Fatalf("deliveries = %d", len(got))
	}
	sig := got[0].Header.Get("X-Atlas-Signature")
	if sig != Sign("s3cret", now.Unix(), []byte(bodies[0])) || got[0].Header.Get("X-Atlas-Event") != "gw.budget.threshold" || !strings.Contains(bodies[0], `"threshold":80`) {
		t.Fatalf("signature/body: %s %s", sig, bodies[0])
	}
	var rows []store.Notification
	db.Order("created_at, id").Find(&rows)
	st := map[string]int{}
	for _, r := range rows {
		st[r.Status]++
		if r.Status == "sent" && r.Attempts != 2 {
			t.Errorf("attempts = %d", r.Attempts)
		}
	}
	if st["sent"] != 1 || st["failed"] != 1 || st["throttled"] != 1 {
		t.Fatalf("records: %v", st)
	}
}

func TestForwardSendsEveryEventSignedWithoutRules(t *testing.T) {
	db, err := store.Open("sqlite", "file:"+filepath.Join(t.TempDir(), "f.db"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Migrate(db, "sqlite"); err != nil {
		t.Fatal(err)
	}
	got := make(chan *http.Request, 2)
	hook := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { got <- r }))
	defer hook.Close()
	u, _ := url.Parse(hook.URL)
	e := &Engine{DB: db, HTTP: hook.Client(), AllowHosts: Hosts(u.Hostname()), Now: time.Now, Forward: &Channel{Type: "webhook", URL: hook.URL, Secret: "k"}}
	e.Handle(context.Background(), "plt.audit.recorded", events.Event{ID: "a1", Time: time.Now(), Data: map[string]any{"org": "acme"}})
	select {
	case r := <-got:
		if r.Header.Get("X-Atlas-Event") != "plt.audit.recorded" || !strings.HasPrefix(r.Header.Get("X-Atlas-Signature"), "t=") {
			t.Fatalf("headers = %v", r.Header)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("forward not delivered")
	}
	var n int64
	db.Model(&store.Notification{}).Count(&n)
	if n != 0 {
		t.Fatalf("forward must not record deliveries, got %d", n)
	}
}

func TestDedupDigestAndEscalation(t *testing.T) {
	db, err := store.Open("sqlite", "file:"+filepath.Join(t.TempDir(), "m4.db"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Migrate(db, "sqlite"); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	got := map[string][]string{}
	hook := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		got[r.URL.Path] = append(got[r.URL.Path], string(b))
		mu.Unlock()
	}))
	defer hook.Close()
	u, _ := url.Parse(hook.URL)
	now := time.Now().UTC()
	clock := func() time.Time { return now }
	e := &Engine{DB: db, HTTP: hook.Client(), AllowHosts: Hosts(u.Hostname()), Now: clock}
	keys, digest := `["alert"]`, "60s"
	esc := `{"steps":[{"afterS":30,"channels":[{"type":"webhook","url":"` + hook.URL + `/secondary"}]}]}`
	db.Create(&store.NotificationRule{ID: "d1", OrgID: "acme", Name: "dedup", Topic: "plt.ob.alert", Channels: `[{"type":"webhook","url":"` + hook.URL + `/dedup"}]`,
		Enabled: true, DedupWindowS: 600, DedupKeys: &keys})
	db.Create(&store.NotificationRule{ID: "g1", OrgID: "acme", Name: "digest", Topic: "plt.mkt.>", Severity: "info", Channels: `[{"type":"webhook","url":"` + hook.URL + `/digest"}]`,
		Enabled: true, Digest: &digest})
	db.Create(&store.NotificationRule{ID: "e1", OrgID: "acme", Name: "oncall", Topic: "plt.sec.>", Severity: "critical", Channels: `[{"type":"webhook","url":"` + hook.URL + `/primary"}]`,
		Enabled: true, Escalation: &esc})
	ctx := context.Background()
	ev := func(subj string, data map[string]any) {
		data["org"] = "acme"
		e.Handle(ctx, subj, events.Event{ID: subj, Time: now, Data: data})
	}
	ev("plt.ob.alert", map[string]any{"alert": "p95", "value": 1.0})
	ev("plt.ob.alert", map[string]any{"alert": "p95", "value": 2.0}) // 같은 지문(alert) → 억제
	ev("plt.ob.alert", map[string]any{"alert": "errors", "value": 3.0})
	for i := 0; i < 3; i++ {
		ev("plt.mkt.installed", map[string]any{"install": i})
	}
	ev("plt.sec.breach", map[string]any{"what": "secret leak"})
	time.Sleep(100 * time.Millisecond)
	mu.Lock()
	if len(got["/dedup"]) != 2 || len(got["/digest"]) != 0 || len(got["/primary"]) != 1 {
		t.Fatalf("immediate = %v", got)
	}
	mu.Unlock()
	var deduped int64
	db.Model(&store.Notification{}).Where("status = ?", "deduped").Count(&deduped)
	now = now.Add(61 * time.Second) // 다이제스트 주기 · 에스컬레이션 30초 경과
	e.FlushDigests(ctx)
	e.EscalateDue(ctx)
	e.EscalateDue(ctx) // 단계는 한 번만
	mu.Lock()
	defer mu.Unlock()
	if deduped != 1 || len(got["/digest"]) != 1 || !strings.Contains(got["/digest"][0], `"count":3`) || len(got["/secondary"]) != 1 {
		t.Fatalf("after = dedup %d %v", deduped, got)
	}
	var root store.Notification
	db.First(&root, "status = ?", "awaiting_ack")
	if root.EscalationStep != 1 {
		t.Fatalf("root = %+v", root)
	}
}
