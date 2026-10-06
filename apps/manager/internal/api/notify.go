package api

import (
	"encoding/json"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"atlas-manager/internal/iam"
	"atlas-manager/internal/notify"
	"atlas-manager/internal/store"
)

// 알림 규칙 (FR-X-NTF-01·02): 조직 admin 이 topic 패턴 + 조건 + 채널(webhook·email) + 스로틀을 정한다.

type ruleBody struct {
	Name      string           `json:"name"`
	Topic     string           `json:"topic"`
	Filter    []notify.Cond    `json:"filter"`
	Severity  string           `json:"severity"`
	Channels  []notify.Channel `json:"channels"`
	ThrottleS int              `json:"throttleS"`
	Enabled   *bool            `json:"enabled"`
	// X-NTF-04·05
	DedupWindowS int                `json:"dedupWindowS"`
	DedupKeys    []string           `json:"dedupKeys"`
	Digest       string             `json:"digest"` // "" | hourly | daily | "<N>s"
	Escalation   *notify.Escalation `json:"escalation"`
}

type ruleView struct {
	store.NotificationRule
	Filter     []notify.Cond      `json:"filter"`
	Channels   []notify.Channel   `json:"channels"`
	DedupKeys  []string           `json:"dedupKeys,omitempty"`
	Escalation *notify.Escalation `json:"escalation,omitempty"`
}

func applyM4(r *store.NotificationRule, b ruleBody) {
	r.DedupWindowS = b.DedupWindowS
	r.DedupKeys, r.Digest, r.Escalation = nil, nil, nil
	if len(b.DedupKeys) > 0 {
		k, _ := json.Marshal(b.DedupKeys)
		ks := string(k)
		r.DedupKeys = &ks
	}
	if b.Digest != "" {
		d := b.Digest
		r.Digest = &d
	}
	if b.Escalation != nil && len(b.Escalation.Steps) > 0 {
		e, _ := json.Marshal(b.Escalation)
		es := string(e)
		r.Escalation = &es
	}
}

func viewRule(r store.NotificationRule) ruleView {
	v := ruleView{NotificationRule: r}
	_ = json.Unmarshal([]byte(r.Filter), &v.Filter)
	_ = json.Unmarshal([]byte(r.Channels), &v.Channels)
	if r.DedupKeys != nil {
		_ = json.Unmarshal([]byte(*r.DedupKeys), &v.DedupKeys)
	}
	if r.Escalation != nil {
		v.Escalation = &notify.Escalation{}
		_ = json.Unmarshal([]byte(*r.Escalation), v.Escalation)
	}
	for i := range v.Channels { // 서명 키는 쓰기 전용
		if v.Channels[i].Secret != "" {
			v.Channels[i].Secret = "***"
		}
	}
	return v
}

func (s *Server) validRule(c *gin.Context, b *ruleBody) bool {
	if b.Name == "" || b.Topic == "" || len(b.Channels) == 0 {
		fail(c, http.StatusBadRequest, "bad_request", "name·topic·channels 필수", nil)
		return false
	}
	if b.Severity == "" {
		b.Severity = "info"
	}
	for _, ch := range b.Channels {
		switch ch.Type {
		case "webhook":
			if s.Notify != nil {
				if err := s.Notify.CheckURL(ch.URL); err != nil {
					fail(c, http.StatusBadRequest, "unsafe_url", err.Error(), nil)
					return false
				}
			}
		case "email":
			if len(ch.To) == 0 {
				fail(c, http.StatusBadRequest, "bad_request", "email 채널은 to 필수", nil)
				return false
			}
		default:
			fail(c, http.StatusBadRequest, "bad_request", "channel type = webhook | email", nil)
			return false
		}
	}
	return true
}

func (s *Server) listRules(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	var rows []store.NotificationRule
	s.db(c).Where("org_id = ?", org).Order("created_at").Find(&rows)
	out := make([]ruleView, 0, len(rows))
	for _, r := range rows {
		out = append(out, viewRule(r))
	}
	c.JSON(http.StatusOK, gin.H{"items": out})
}

func (s *Server) createRule(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "admin", orgObj(org)) {
		return
	}
	var b ruleBody
	if err := c.ShouldBindJSON(&b); err != nil || !s.validRule(c, &b) {
		if !c.IsAborted() {
			fail(c, http.StatusBadRequest, "bad_request", "본문 오류", nil)
		}
		return
	}
	f, _ := json.Marshal(b.Filter)
	ch, _ := json.Marshal(b.Channels)
	r := store.NotificationRule{ID: iam.ULID(), OrgID: org, Name: b.Name, Topic: b.Topic, Filter: string(f), Severity: b.Severity,
		Channels: string(ch), ThrottleS: b.ThrottleS, Enabled: b.Enabled == nil || *b.Enabled, CreatedAt: time.Now().UTC()}
	applyM4(&r, b)
	s.tenantScope(c, org)
	if err := s.db(c).Create(&r).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	s.audit(c, org, "notification.rule.create", "notification_rule:"+r.ID, "success", nil, gin.H{"topic": r.Topic, "name": r.Name}, nil)
	c.JSON(http.StatusCreated, viewRule(r))
}

func (s *Server) findRule(c *gin.Context, org string) (store.NotificationRule, bool) {
	var r store.NotificationRule
	s.tenantScope(c, org)
	if err := s.db(c).First(&r, "id = ? AND org_id = ?", c.Param("id"), org).Error; err != nil {
		fail(c, http.StatusNotFound, "not_found", "규칙 없음", nil)
		return r, false
	}
	return r, true
}

func (s *Server) updateRule(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "admin", orgObj(org)) {
		return
	}
	r, ok := s.findRule(c, org)
	if !ok {
		return
	}
	var b ruleBody
	if err := c.ShouldBindJSON(&b); err != nil {
		fail(c, http.StatusBadRequest, "bad_request", err.Error(), nil)
		return
	}
	// 서명 키 "***" 는 기존 값 유지
	var old []notify.Channel
	_ = json.Unmarshal([]byte(r.Channels), &old)
	for i := range b.Channels {
		if b.Channels[i].Secret == "***" && i < len(old) {
			b.Channels[i].Secret = old[i].Secret
		}
	}
	if !s.validRule(c, &b) {
		return
	}
	f, _ := json.Marshal(b.Filter)
	ch, _ := json.Marshal(b.Channels)
	before := gin.H{"topic": r.Topic, "enabled": r.Enabled}
	r.Name, r.Topic, r.Filter, r.Severity, r.Channels, r.ThrottleS = b.Name, b.Topic, string(f), b.Severity, string(ch), b.ThrottleS
	applyM4(&r, b)
	if b.Enabled != nil {
		r.Enabled = *b.Enabled
	}
	if err := s.db(c).Save(&r).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	s.audit(c, org, "notification.rule.update", "notification_rule:"+r.ID, "success", before, gin.H{"topic": r.Topic, "enabled": r.Enabled}, nil)
	c.JSON(http.StatusOK, viewRule(r))
}

func (s *Server) deleteRule(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "admin", orgObj(org)) {
		return
	}
	r, ok := s.findRule(c, org)
	if !ok {
		return
	}
	s.db(c).Delete(&r)
	s.audit(c, org, "notification.rule.delete", "notification_rule:"+r.ID, "success", gin.H{"topic": r.Topic}, nil, nil)
	c.Status(http.StatusNoContent)
}

func (s *Server) testRule(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "admin", orgObj(org)) {
		return
	}
	r, ok := s.findRule(c, org)
	if !ok {
		return
	}
	if s.Notify == nil {
		fail(c, http.StatusServiceUnavailable, "notify_disabled", "알림 엔진 비활성", nil)
		return
	}
	c.JSON(http.StatusOK, gin.H{"deliveries": s.Notify.Test(c, r)})
}

func (s *Server) listDeliveries(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	q := s.db(c).Where("org_id = ?", org)
	for k, col := range map[string]string{"rule": "rule_id", "status": "status", "topic": "topic"} {
		if v := c.Query(k); v != "" {
			q = q.Where(col+" = ?", v)
		}
	}
	var rows []store.Notification
	q.Order("created_at DESC").Limit(200).Find(&rows)
	c.JSON(http.StatusOK, gin.H{"items": rows})
}

// ackNotification: POST /api/v1/orgs/{org}/notifications/{id}/ack — 에스컬레이션 중지 (FR-X-NTF-05).
func (s *Server) ackNotification(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	var n store.Notification
	if s.db(c).First(&n, "id = ? AND org_id = ?", c.Param("id"), org).Error != nil {
		fail(c, http.StatusNotFound, "not_found", "알림 없음", nil)
		return
	}
	if n.AckedAt != nil {
		c.JSON(http.StatusOK, n)
		return
	}
	now, who := time.Now().UTC(), firstNonEmpty(principal(c).Sub, "anonymous")
	s.db(c).Model(&store.Notification{}).Where("id = ?", n.ID).Updates(map[string]any{"acked_at": now, "acked_by": who, "status": "acked"})
	s.audit(c, org, "notification.ack", "notification:"+n.ID, "success", nil, gin.H{"by": who, "step": n.EscalationStep}, nil)
	n.AckedAt, n.AckedBy, n.Status = &now, &who, "acked"
	c.JSON(http.StatusOK, n)
}
