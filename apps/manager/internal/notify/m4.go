package notify

// 알림 피로 방지·온콜 (FR-X-NTF-04·05):
//   - 중복 억제: 규칙 dedupWindowS 안에서 지문(규칙·subject·dedupKeys 값)이 같은 이벤트는 보내지 않고 deduped 로 기록.
//   - 다이제스트: 규칙 digest(hourly·daily·"<N>s") 면 즉시 보내지 않고 모았다가 주기마다 한 통으로.
//   - 에스컬레이션: escalation.steps[{afterS|afterMin, channels}] — 확인(ack) 없으면 단계별로 다음 대상에게.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"atlas-manager/internal/iam"
	"atlas-manager/internal/store"
)

// EscalationStep 은 확인되지 않은 알림의 다음 단계다.
type EscalationStep struct {
	AfterS   int       `json:"afterS,omitempty"`
	AfterMin int       `json:"afterMin,omitempty"`
	Channels []Channel `json:"channels"`
}

// Escalation 은 단계 목록이다.
type Escalation struct {
	Steps []EscalationStep `json:"steps"`
}

func (s EscalationStep) after() time.Duration {
	return time.Duration(s.AfterS)*time.Second + time.Duration(s.AfterMin)*time.Minute
}

// Fingerprint 는 중복 판정 지문이다: 규칙·subject·(dedupKeys 의 값, 없으면 data 전체의 스칼라 값).
func Fingerprint(ruleID, subject string, data map[string]any, keys []string) string {
	if len(keys) == 0 {
		for k, v := range data {
			switch v.(type) {
			case string, float64, bool, int, int64:
				keys = append(keys, k)
			}
		}
	}
	sort.Strings(keys)
	parts := []string{ruleID, subject}
	for _, k := range keys {
		parts = append(parts, k+"="+fmt.Sprint(lookup(data, k)))
	}
	h := sha256.Sum256([]byte(strings.Join(parts, "|")))
	return hex.EncodeToString(h[:16])
}

func digestPeriod(d string) time.Duration {
	switch d {
	case "hourly":
		return time.Hour
	case "daily":
		return 24 * time.Hour
	}
	if strings.HasSuffix(d, "s") {
		if n, err := strconv.Atoi(strings.TrimSuffix(d, "s")); err == nil && n > 0 {
			return time.Duration(n) * time.Second
		}
	}
	return 0
}

// preDeliver 는 중복 억제·다이제스트를 처리한다. skip 이면 즉시 전달하지 않는다. fp = 전달 기록에 남길 지문.
func (e *Engine) preDeliver(ctx context.Context, r store.NotificationRule, subject string, msg Message) (skip bool, fp string) {
	if r.DedupWindowS > 0 {
		var keys []string
		if r.DedupKeys != nil {
			_ = json.Unmarshal([]byte(*r.DedupKeys), &keys)
		}
		fp = Fingerprint(r.ID, subject, msg.Data, keys)
		var n int64
		e.DB.WithContext(ctx).Model(&store.Notification{}).Where("rule_id = ? AND fingerprint = ? AND status <> ? AND created_at > ?", r.ID, fp, "deduped",
			e.Now().UTC().Add(-time.Duration(r.DedupWindowS)*time.Second)).Count(&n)
		if n > 0 {
			e.recordX(ctx, r, subject, "-", "", "deduped", &fp, nil)
			return true, fp
		}
	}
	if r.Digest != nil && digestPeriod(*r.Digest) > 0 {
		b, _ := json.Marshal(msg)
		p := string(b)
		e.recordX(ctx, r, subject, "-", "", "digest_pending", nil, &p)
		return true, fp
	}
	return false, fp
}

func (e *Engine) recordX(ctx context.Context, r store.NotificationRule, subject, channel, target, status string, fp, payload *string) store.Notification {
	n := store.Notification{ID: iam.ULID(), OrgID: r.OrgID, RuleID: r.ID, Topic: subject, Channel: channel, Target: target, Status: status,
		CreatedAt: e.Now().UTC(), Fingerprint: fp, Payload: payload}
	e.DB.WithContext(ctx).Create(&n)
	return n
}

// FlushDigests 는 기간이 찬 다이제스트를 한 통으로 보낸다 (주기 실행).
func (e *Engine) FlushDigests(ctx context.Context) {
	var rules []store.NotificationRule
	e.DB.WithContext(ctx).Where("enabled = ? AND digest IS NOT NULL AND digest <> ''", true).Find(&rules)
	for _, r := range rules {
		period := digestPeriod(*r.Digest)
		var pend []store.Notification
		e.DB.WithContext(ctx).Where("rule_id = ? AND status = ?", r.ID, "digest_pending").Order("created_at").Find(&pend)
		if len(pend) == 0 || e.Now().UTC().Sub(pend[0].CreatedAt) < period {
			continue
		}
		items, topics := []map[string]any{}, map[string]int{}
		for _, p := range pend {
			topics[p.Topic]++
			if len(items) < 20 && p.Payload != nil {
				var m Message
				if json.Unmarshal([]byte(*p.Payload), &m) == nil {
					items = append(items, map[string]any{"topic": m.Topic, "time": m.Time, "data": m.Data})
				}
			}
		}
		msg := Message{ID: iam.ULID(), Topic: "digest", Severity: r.Severity, Org: r.OrgID, Rule: r.Name, Time: e.Now().UTC(),
			Data: map[string]any{"count": len(pend), "topics": topics, "items": items, "period": *r.Digest}}
		var chans []Channel
		_ = json.Unmarshal([]byte(r.Channels), &chans)
		for _, ch := range chans {
			n, err := e.deliver(ctx, ch, msg)
			st := "sent"
			if err != nil {
				st = "failed"
			}
			e.record(ctx, r, "digest", ch, st, n, err)
		}
		ids := make([]string, 0, len(pend))
		for _, p := range pend {
			ids = append(ids, p.ID)
		}
		e.DB.WithContext(ctx).Model(&store.Notification{}).Where("id IN ?", ids).Update("status", "digested")
	}
}

// EscalateDue 는 확인되지 않은 에스컬레이션 대상을 다음 단계로 보낸다 (주기 실행).
func (e *Engine) EscalateDue(ctx context.Context) {
	var roots []store.Notification
	e.DB.WithContext(ctx).Where("status = ? AND acked_at IS NULL", "awaiting_ack").Find(&roots)
	for _, n := range roots {
		var r store.NotificationRule
		if e.DB.WithContext(ctx).First(&r, "id = ?", n.RuleID).Error != nil || r.Escalation == nil {
			continue
		}
		var esc Escalation
		_ = json.Unmarshal([]byte(*r.Escalation), &esc)
		if n.EscalationStep >= len(esc.Steps) {
			continue
		}
		step := esc.Steps[n.EscalationStep]
		if e.Now().UTC().Sub(n.CreatedAt) < step.after() {
			continue
		}
		var msg Message
		if n.Payload != nil {
			_ = json.Unmarshal([]byte(*n.Payload), &msg)
		}
		msg.Severity = "critical"
		if msg.Data == nil {
			msg.Data = map[string]any{}
		}
		msg.Data["escalation_step"] = n.EscalationStep + 1
		msg.Data["ack_url"] = "/api/v1/orgs/" + n.OrgID + "/notifications/" + n.ID + "/ack"
		for _, ch := range step.Channels {
			k, err := e.deliver(ctx, ch, msg)
			st := "sent"
			if err != nil {
				st = "failed"
			}
			e.record(ctx, r, n.Topic+"#escalation", ch, st, k, err)
		}
		e.DB.WithContext(ctx).Model(&store.Notification{}).Where("id = ?", n.ID).Update("escalation_step", n.EscalationStep+1)
	}
}
