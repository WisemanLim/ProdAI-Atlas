// Package notify 는 이벤트 구독·규칙·다채널 전달이다 (FR-X-NTF-01·02·03):
// NATS(plt.* · gw.*) 이벤트 → 조직 규칙(topic 패턴 + 필터 + 스로틀) → 웹훅(HMAC 서명, generic·slack 형식) · 이메일(SMTP).
package notify

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/smtp"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"gorm.io/gorm"

	"atlas-manager/internal/events"
	"atlas-manager/internal/iam"
	"atlas-manager/internal/store"
)

type Cond struct {
	Field string `json:"field"` // data 의 점 경로 (예: threshold, scope)
	Op    string `json:"op"`    // == != > >= < <= in contains
	Value any    `json:"value"`
}

type Channel struct {
	Type   string   `json:"type"` // webhook | email
	URL    string   `json:"url,omitempty"`
	Secret string   `json:"secret,omitempty"` // HMAC 키 (응답에서는 가린다)
	Format string   `json:"format,omitempty"` // generic | slack
	To     []string `json:"to,omitempty"`
}

type Engine struct {
	DB         *gorm.DB
	HTTP       *http.Client
	SMTPAddr   string
	From       string
	AllowHosts map[string]bool
	Now        func() time.Time
	// Forward 는 1st-party 수집기(X-RPT)다 — 규칙과 무관하게 모든 PLT·GW 이벤트를 서명 webhook 으로 넘긴다(전달 기록 없음, FR-X-RPT-01·04).
	Forward *Channel
	mu      sync.Mutex
	last    map[string]time.Time
}

// Topic 은 NATS 와일드카드 매칭이다: * = 토큰 하나, > = 나머지 전부.
func Topic(pattern, subject string) bool {
	p, s := strings.Split(pattern, "."), strings.Split(subject, ".")
	for i, t := range p {
		if t == ">" {
			return len(s) > i
		}
		if i >= len(s) || (t != "*" && t != s[i]) {
			return false
		}
	}
	return len(p) == len(s)
}

func lookup(data map[string]any, path string) (any, bool) {
	var cur any = data
	for _, k := range strings.Split(path, ".") {
		m, ok := cur.(map[string]any)
		if !ok {
			return nil, false
		}
		if cur, ok = m[k]; !ok {
			return nil, false
		}
	}
	return cur, true
}

func num(v any) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, true
	case int:
		return float64(x), true
	case json.Number:
		f, err := x.Float64()
		return f, err == nil
	case string:
		f, err := strconv.ParseFloat(x, 64)
		return f, err == nil
	}
	return 0, false
}

// Match 는 모든 조건을 만족하는지 본다 (CEL 대신 선언형 조건 목록 — 평가 엔진 의존 없이 안전하게).
func Match(conds []Cond, data map[string]any) bool {
	for _, c := range conds {
		v, ok := lookup(data, c.Field)
		if !ok {
			return false
		}
		a, an := num(v)
		b, bn := num(c.Value)
		switch c.Op {
		case "==", "":
			if fmt.Sprint(v) != fmt.Sprint(c.Value) {
				return false
			}
		case "!=":
			if fmt.Sprint(v) == fmt.Sprint(c.Value) {
				return false
			}
		case ">", ">=", "<", "<=":
			if !an || !bn || !map[string]bool{">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[c.Op] {
				return false
			}
		case "in":
			list, _ := c.Value.([]any)
			hit := false
			for _, x := range list {
				if fmt.Sprint(x) == fmt.Sprint(v) {
					hit = true
				}
			}
			if !hit {
				return false
			}
		case "contains":
			if !strings.Contains(fmt.Sprint(v), fmt.Sprint(c.Value)) {
				return false
			}
		default:
			return false
		}
	}
	return true
}

var ErrUnsafeURL = errors.New("unsafe webhook url")

// CheckURL 은 SSRF 차단이다: http(s) 만, 사설·루프백·링크로컬은 허용 목록 호스트만.
func (e *Engine) CheckURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" {
		return fmt.Errorf("%w: http(s) URL 필요", ErrUnsafeURL)
	}
	if e.AllowHosts[u.Hostname()] {
		return nil
	}
	if u.Scheme != "https" {
		return fmt.Errorf("%w: 허용 목록 밖 webhook 은 https 만", ErrUnsafeURL)
	}
	ips, err := net.LookupIP(u.Hostname())
	if err != nil {
		return fmt.Errorf("%w: 호스트 해석 실패", ErrUnsafeURL)
	}
	for _, ip := range ips {
		if ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsUnspecified() || ip.IsMulticast() {
			return fmt.Errorf("%w: 내부 주소 %s", ErrUnsafeURL, ip)
		}
	}
	return nil
}

// OrgOf 는 이벤트가 속한 조직이다: data.org > data.tenant > (scope=org 면) scope_id > default.
func OrgOf(data map[string]any) string {
	for _, k := range []string{"org", "tenant"} {
		if v, ok := data[k].(string); ok && v != "" {
			return v
		}
	}
	if data["scope"] == "org" {
		if v, ok := data["scope_id"].(string); ok {
			return v
		}
	}
	return "default"
}

// Handle 은 이벤트 하나를 규칙에 대어 보고 전달한다.
func (e *Engine) Handle(ctx context.Context, subject string, ev events.Event) {
	data, _ := ev.Data.(map[string]any)
	if data == nil {
		data = map[string]any{}
	}
	org := OrgOf(data)
	if e.Forward != nil {
		m := Message{ID: ev.ID, Topic: subject, Severity: "info", Org: org, Rule: "x-rpt-ingest", Time: ev.Time, Data: data}
		go func() { // 소비 루프를 막지 않는다 — 수집기는 원천 id 로 멱등
			if _, err := e.deliver(context.Background(), *e.Forward, m); err != nil {
				slog.Warn("rpt forward failed", "subject", subject, "err", err)
			}
		}()
	}
	var rules []store.NotificationRule
	if err := e.DB.WithContext(ctx).Where("org_id = ? AND enabled = ?", org, true).Find(&rules).Error; err != nil {
		return
	}
	for _, r := range rules {
		if !Topic(r.Topic, subject) {
			continue
		}
		var conds []Cond
		_ = json.Unmarshal([]byte(r.Filter), &conds)
		if !Match(conds, data) {
			continue
		}
		var chans []Channel
		_ = json.Unmarshal([]byte(r.Channels), &chans)
		if e.throttled(r, subject) {
			e.record(ctx, r, subject, Channel{Type: "-"}, "throttled", 0, nil)
			continue
		}
		msg := Message{ID: ev.ID, Topic: subject, Severity: r.Severity, Org: org, Rule: r.Name, Time: ev.Time, Data: data}
		skip, fp := e.preDeliver(ctx, r, subject, msg) // 중복 억제·다이제스트 (X-NTF-04)
		if skip {
			continue
		}
		for _, ch := range chans {
			n, err := e.deliver(ctx, ch, msg)
			st := "sent"
			if err != nil {
				st = "failed"
			}
			e.recordFP(ctx, r, subject, ch, st, n, err, fp)
		}
		if r.Escalation != nil && *r.Escalation != "" { // 확인 대기 기록 — 미확인 시 EscalateDue 가 다음 단계로 (X-NTF-05)
			b, _ := json.Marshal(msg)
			p := string(b)
			e.recordX(ctx, r, subject, "-", "", "awaiting_ack", nil, &p)
		}
	}
}

func (e *Engine) throttled(r store.NotificationRule, subject string) bool {
	if r.ThrottleS <= 0 {
		return false
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.last == nil {
		e.last = map[string]time.Time{}
	}
	k := r.ID + "|" + subject
	now := e.Now()
	if t, ok := e.last[k]; ok && now.Sub(t) < time.Duration(r.ThrottleS)*time.Second {
		return true
	}
	e.last[k] = now
	return false
}

func (e *Engine) record(ctx context.Context, r store.NotificationRule, subject string, ch Channel, status string, attempts int, err error) {
	e.recordFP(ctx, r, subject, ch, status, attempts, err, "")
}

func (e *Engine) recordFP(ctx context.Context, r store.NotificationRule, subject string, ch Channel, status string, attempts int, err error, fp string) {
	target := ch.URL
	if ch.Type == "email" {
		target = strings.Join(ch.To, ",")
	}
	n := store.Notification{ID: iam.ULID(), OrgID: r.OrgID, RuleID: r.ID, Topic: subject, Channel: ch.Type, Target: target, Status: status,
		Attempts: attempts, CreatedAt: e.Now().UTC()}
	if fp != "" {
		n.Fingerprint = &fp
	}
	if err != nil {
		s := err.Error()
		n.Error = &s
	}
	e.DB.WithContext(ctx).Create(&n)
}

type Message struct {
	ID       string         `json:"id"`
	Topic    string         `json:"topic"`
	Severity string         `json:"severity"`
	Org      string         `json:"org"`
	Rule     string         `json:"rule"`
	Time     time.Time      `json:"time"`
	Data     map[string]any `json:"data"`
}

func (m Message) Text() string {
	b, _ := json.Marshal(m.Data)
	return fmt.Sprintf("[Atlas %s] %s — %s (%s)\n%s", strings.ToUpper(m.Severity), m.Rule, m.Topic, m.Org, b)
}

// Sign 은 X-Atlas-Signature 값이다: t=<unix>,v1=hex(HMAC-SHA256(secret, "<t>.<body>")).
func Sign(secret string, t int64, body []byte) string {
	h := hmac.New(sha256.New, []byte(secret))
	fmt.Fprintf(h, "%d.", t)
	h.Write(body)
	return fmt.Sprintf("t=%d,v1=%s", t, hex.EncodeToString(h.Sum(nil)))
}

func (e *Engine) deliver(ctx context.Context, ch Channel, m Message) (int, error) {
	switch ch.Type {
	case "webhook":
		if err := e.CheckURL(ch.URL); err != nil {
			return 0, err
		}
		var body []byte
		if ch.Format == "slack" {
			body, _ = json.Marshal(map[string]any{"text": m.Text()})
		} else {
			body, _ = json.Marshal(m)
		}
		var last error
		for i := 1; i <= 3; i++ {
			req, _ := http.NewRequestWithContext(ctx, http.MethodPost, ch.URL, bytes.NewReader(body))
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("X-Atlas-Event", m.Topic)
			if ch.Secret != "" {
				req.Header.Set("X-Atlas-Signature", Sign(ch.Secret, e.Now().Unix(), body))
			}
			res, err := e.HTTP.Do(req)
			if err == nil {
				res.Body.Close()
				if res.StatusCode < 300 {
					return i, nil
				}
				last = fmt.Errorf("webhook %d", res.StatusCode)
				if res.StatusCode < 500 && res.StatusCode != 429 {
					return i, last
				}
			} else {
				last = err
			}
			time.Sleep(time.Duration(i*i) * 200 * time.Millisecond)
		}
		return 3, last
	case "email":
		if e.SMTPAddr == "" || len(ch.To) == 0 {
			return 0, errors.New("SMTP 미설정 또는 수신자 없음")
		}
		msg := fmt.Sprintf("From: %s\r\nTo: %s\r\nSubject: [Atlas %s] %s — %s\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n%s\r\n",
			e.From, strings.Join(ch.To, ", "), strings.ToUpper(m.Severity), m.Rule, m.Topic, m.Text())
		return 1, smtp.SendMail(e.SMTPAddr, nil, e.From, ch.To, []byte(msg))
	}
	return 0, fmt.Errorf("알 수 없는 채널 %s", ch.Type)
}

// Run 은 PLT·GW 스트림을 소비한다.
func (e *Engine) Run(ctx context.Context, js *events.JetStream) {
	for _, s := range []string{"PLT", "GW", "AUDIT"} {
		go js.Consume(ctx, s, "atlas-notifier-"+strings.ToLower(s), func(subject string, ev events.Event) { e.Handle(ctx, subject, ev) })
	}
	slog.Info("notifier started", "streams", "PLT,GW,AUDIT")
}

// Template 은 자주 쓰는 규칙의 출발점이다 (FR-X-NTF-03: 예산 임계·쿼터 초과·승인 대기).
type Template struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Topic    string `json:"topic"`
	Filter   []Cond `json:"filter"`
	Severity string `json:"severity"`
}

var Templates = []Template{
	{ID: "budget-80", Name: "예산 80% 도달", Topic: "gw.budget.threshold", Filter: []Cond{{Field: "threshold", Op: ">=", Value: 80}}, Severity: "warning"},
	{ID: "budget-100", Name: "예산 소진", Topic: "gw.budget.threshold", Filter: []Cond{{Field: "threshold", Op: ">=", Value: 100}}, Severity: "critical"},
	{ID: "quota-exceeded", Name: "쿼터 초과", Topic: "plt.quota.exceeded", Severity: "warning"},
	{ID: "approval-pending", Name: "승인 대기 (고위험)", Topic: "plt.approval.requested", Filter: []Cond{{Field: "risk", Op: "in", Value: []any{"high", "critical"}}}, Severity: "warning"},
	{ID: "approval-expired", Name: "승인 만료", Topic: "plt.approval.expired", Severity: "info"},
}

// Test 는 규칙 하나를 합성 이벤트로 즉시 발송한다(스로틀 무시).
func (e *Engine) Test(ctx context.Context, r store.NotificationRule) []store.Notification {
	var chans []Channel
	_ = json.Unmarshal([]byte(r.Channels), &chans)
	subject := strings.ReplaceAll(strings.ReplaceAll(r.Topic, ">", "test"), "*", "test")
	msg := Message{ID: "test-" + iam.ULID(), Topic: subject, Severity: r.Severity, Org: r.OrgID, Rule: r.Name, Time: e.Now().UTC(), Data: map[string]any{"test": true}}
	var out []store.Notification
	for _, ch := range chans {
		n, err := e.deliver(ctx, ch, msg)
		st := map[bool]string{true: "sent", false: "failed"}[err == nil]
		e.record(ctx, r, subject, ch, "test:"+st, n, err)
		row := store.Notification{RuleID: r.ID, Topic: subject, Channel: ch.Type, Status: "test:" + st, Attempts: n}
		if err != nil {
			s := err.Error()
			row.Error = &s
		}
		out = append(out, row)
	}
	return out
}

// Hosts 는 쉼표 목록을 허용 호스트 집합으로 바꾼다.
func Hosts(csv string) map[string]bool {
	m := map[string]bool{}
	for _, h := range strings.Split(csv, ",") {
		if h = strings.TrimSpace(h); h != "" {
			m[h] = true
		}
	}
	return m
}
