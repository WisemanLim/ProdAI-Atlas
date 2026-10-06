package api

// 승인 정책 (FR-X-HITL-03·04·05 · GR-POL-06):
//   - 규칙: action glob + 조건(payload 필드) → require|deny, 승인자(role:<관계>·user:<주체>), quorum(다단·2인), SLA(초) + 에스컬레이션 대상.
//   - 실제 행위 미리보기: requirePreviewFor 위험도면 payload.action_preview(human_summary·structured_args) 또는 구조화 인자가 있어야 생성된다 —
//     LLM 설명 문장만으로는 승인 요청을 만들 수 없다. 미리보기는 승인 화면에 그대로 보인다.
//   - 결정: 승인자 1인 = approval_decisions 1행(서명 = sha256(승인자·시각·payload 해시·결정)), quorum 에 닿으면 확정. 반려는 즉시 확정.
//   - 위임: 부재 중 대리인이 원 승인자 몫으로 결정(on_behalf 기록).
//   - SLA: 대기 sla_seconds 초과 → plt.approval.escalated (X-NTF 가 escalateTo 로 전달), 만료 → 거부(기존).

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"path"
	"sort"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/gorm"

	"atlas-manager/internal/notify"
	"atlas-manager/internal/store"
)

type approvalRule struct {
	Name       string        `json:"name"`
	Match      string        `json:"match"` // action glob (예: payments.*, *.delete)
	Risk       []string      `json:"risk,omitempty"`
	When       []notify.Cond `json:"when,omitempty"`
	Action     string        `json:"action"` // require | deny
	Approvers  []string      `json:"approvers,omitempty"`
	Quorum     int           `json:"quorum,omitempty"`
	SlaSeconds int           `json:"slaSeconds,omitempty"`
	SlaMinutes int           `json:"slaMinutes,omitempty"`
	EscalateTo []string      `json:"escalateTo,omitempty"`
}

type approvalPolicy struct {
	Rules             []approvalRule `json:"rules"`
	RequirePreviewFor []string       `json:"requirePreviewFor"` // 위험도 — 정책 저장 시 생략하면 [high, critical], [] 이면 끔
	HitlFor           []string       `json:"hitlFor"`           // 승인 대상 도구 분류 기본값 (evaluate 용)
}

var defaultApprovalPolicy = approvalPolicy{RequirePreviewFor: []string{"high", "critical"}, HitlFor: []string{"payments", "deletes", "external_email"}}

func (s *Server) approvalPolicyFor(db *gorm.DB, org string) approvalPolicy {
	var row store.ApprovalPolicy
	if db.Where("org_id = ?", org).Limit(1).Find(&row).RowsAffected == 0 {
		// 정책이 없는 조직 = 기존 계약 유지(미리보기 강제 없음). 조직이 정책을 저장하면 기본 [high, critical] 이 켜진다 (opt-in)
		return approvalPolicy{HitlFor: defaultApprovalPolicy.HitlFor}
	}
	p := defaultApprovalPolicy
	_ = json.Unmarshal([]byte(row.Policy), &p)
	return p
}

func (r approvalRule) sla() int { return r.SlaSeconds + r.SlaMinutes*60 }

func matchApprovalRule(p approvalPolicy, action, risk string, payload map[string]any) *approvalRule {
	for i := range p.Rules {
		r := p.Rules[i]
		ok, _ := path.Match(r.Match, action)
		if !ok && r.Match != "*" && !strings.EqualFold(r.Match, action) {
			continue
		}
		if len(r.Risk) > 0 && !contains(r.Risk, risk) {
			continue
		}
		if !notify.Match(r.When, payload) {
			continue
		}
		return &p.Rules[i]
	}
	return nil
}

func contains(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}

// derivePreview 는 action_preview 를 쓰거나 구조화 인자(tool·arguments·calls)에서 만든다. 둘 다 없으면 nil.
func derivePreview(payload map[string]any) map[string]any {
	if pv, ok := payload["action_preview"].(map[string]any); ok && pv["human_summary"] != nil && pv["structured_args"] != nil {
		return pv
	}
	var calls []any
	if cs, ok := payload["calls"].([]any); ok && len(cs) > 0 {
		calls = cs
	} else if payload["arguments"] != nil || payload["tool"] != nil {
		calls = []any{map[string]any{"tool": payload["tool"], "arguments": payload["arguments"]}}
	}
	if len(calls) == 0 {
		return nil
	}
	parts := []string{}
	for _, c := range calls {
		m, _ := c.(map[string]any)
		b, _ := json.Marshal(m["arguments"])
		parts = append(parts, fmt.Sprintf("%v(%s)", firstNonNil(m["tool"], m["name"]), truncate(string(b), 200)))
	}
	return map[string]any{"human_summary": strings.Join(parts, "; "), "structured_args": calls, "derived": true}
}

func firstNonNil(v ...any) any {
	for _, x := range v {
		if x != nil {
			return x
		}
	}
	return nil
}

func truncate(s string, n int) string {
	if len(s) > n {
		return s[:n] + "…"
	}
	return s
}

// applyApprovalPolicy 는 생성 직전 승인 요청에 정책을 적용한다. 거부 사유가 있으면 (status, code, message).
func (s *Server) applyApprovalPolicy(db *gorm.DB, ap *store.Approval, payload map[string]any) (int, string, string) {
	p := s.approvalPolicyFor(db, ap.OrgID)
	if contains(p.RequirePreviewFor, ap.RiskLevel) {
		pv := derivePreview(payload)
		if pv == nil {
			return http.StatusUnprocessableEntity, "preview_required", "위험도 " + ap.RiskLevel + " 승인 요청은 실제 행위 미리보기(action_preview 또는 구조화 인자) 필요 — LLM 설명만으로는 불가"
		}
		b, _ := json.Marshal(pv)
		ps := string(b)
		ap.Preview = &ps
	}
	ap.Quorum = 1
	r := matchApprovalRule(p, ap.Action, ap.RiskLevel, payload)
	if r == nil {
		return 0, "", ""
	}
	if r.Action == "deny" {
		return http.StatusForbidden, "policy_denied", "승인 정책 " + r.Name + " 이 이 행위를 금지"
	}
	name := r.Name
	ap.PolicyRule = &name
	if len(r.Approvers) > 0 {
		b, _ := json.Marshal(r.Approvers)
		a := string(b)
		ap.Approvers = &a
	}
	if r.Quorum > 1 {
		ap.Quorum = r.Quorum
	}
	ap.SlaSeconds = r.sla()
	if len(r.EscalateTo) > 0 {
		b, _ := json.Marshal(r.EscalateTo)
		e := string(b)
		ap.EscalateTo = &e
	}
	return 0, "", ""
}

func (s *Server) getApprovalPolicy(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	c.JSON(http.StatusOK, gin.H{"org": org, "policy": s.approvalPolicyFor(s.db(c), org)})
}

func (s *Server) putApprovalPolicy(c *gin.Context) {
	org := c.Param("org")
	if _, ok := s.orgGuard(c, org); !ok || !s.require(c, "admin", orgObj(org)) {
		return
	}
	var p approvalPolicy
	if err := c.ShouldBindJSON(&p); err != nil {
		fail(c, http.StatusBadRequest, "bad_request", err.Error(), nil)
		return
	}
	for _, r := range p.Rules {
		if r.Name == "" || r.Match == "" || (r.Action != "require" && r.Action != "deny") {
			fail(c, http.StatusUnprocessableEntity, "invalid_policy", "rule = {name, match, action: require|deny}", gin.H{"rule": r})
			return
		}
		for _, a := range r.Approvers {
			if !strings.HasPrefix(a, "role:") && !strings.HasPrefix(a, "user:") {
				fail(c, http.StatusUnprocessableEntity, "invalid_policy", "approvers = role:<관계> | user:<주체>", gin.H{"approver": a})
				return
			}
		}
	}
	if p.RequirePreviewFor == nil {
		p.RequirePreviewFor = defaultApprovalPolicy.RequirePreviewFor
	}
	b, _ := json.Marshal(p)
	s.tenantScope(c, org)
	s.db(c).Save(&store.ApprovalPolicy{OrgID: org, Policy: string(b), UpdatedAt: time.Now().UTC()})
	s.audit(c, org, "approval.policy.update", "approval_policy:"+org, "success", nil, gin.H{"rules": len(p.Rules)}, nil)
	c.JSON(http.StatusOK, gin.H{"org": org, "policy": p})
}

// evaluateApproval: POST /api/v1/approvals/evaluate — 에이전트·도구 실행기가 "이 행위에 승인이 필요한가" 를 묻는다.
func (s *Server) evaluateApproval(c *gin.Context) {
	org := orgOf(c)
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	var b struct {
		Action    string         `json:"action"`
		RiskLevel string         `json:"riskLevel"`
		Category  string         `json:"category"`
		Payload   map[string]any `json:"payload"`
	}
	if err := c.ShouldBindJSON(&b); err != nil || b.Action == "" {
		fail(c, http.StatusBadRequest, "bad_request", "action 필수", nil)
		return
	}
	p := s.approvalPolicyFor(s.db(c), org)
	r := matchApprovalRule(p, b.Action, b.RiskLevel, b.Payload)
	required := r != nil && r.Action == "require" || contains(p.HitlFor, b.Category)
	out := gin.H{"action": b.Action, "required": required, "denied": r != nil && r.Action == "deny", "previewRequired": contains(p.RequirePreviewFor, b.RiskLevel)}
	if r != nil {
		out["rule"], out["approvers"], out["quorum"], out["slaSeconds"] = r.Name, r.Approvers, max(1, r.Quorum), r.sla()
	}
	c.JSON(http.StatusOK, out)
}

// ---- 다단 승인·위임 ----

// eligible 은 이 주체가 승인자 목록에 들거나(직접) 승인자 user 의 유효한 대리인인지 본다. 반환 = (자격, 대리 대상).
func (s *Server) eligible(c *gin.Context, ap store.Approval, who string) (bool, string) {
	if ap.Approvers == nil || *ap.Approvers == "" {
		return true, ""
	}
	var specs []string
	_ = json.Unmarshal([]byte(*ap.Approvers), &specs)
	p := principal(c)
	for _, sp := range specs {
		switch {
		case strings.HasPrefix(sp, "role:") && s.authOn() && s.can(c, strings.TrimPrefix(sp, "role:"), projObj(ap.OrgID, ap.Project)):
			return true, ""
		case strings.HasPrefix(sp, "role:") && !s.authOn():
			return true, ""
		case sp == "user:"+who || sp == "user:"+p.Name:
			return true, ""
		}
	}
	var dels []store.ApprovalDelegation
	s.DB.WithContext(c).Where("org_id = ? AND delegate = ? AND until > ?", ap.OrgID, who, time.Now().UTC()).Find(&dels)
	for _, d := range dels {
		if contains(specs, "user:"+d.Principal) {
			return true, d.Principal
		}
	}
	return false, ""
}

func decisionSignature(who string, at time.Time, payloadHash, decision string) string {
	h := sha256.Sum256([]byte(who + "|" + at.Format(time.RFC3339Nano) + "|" + payloadHash + "|" + decision))
	return hex.EncodeToString(h[:])
}

// recordDecision 은 1인 결정을 남기고, quorum 미달이면 (false, 현재 승인 수) 를 돌려준다.
func (s *Server) recordDecision(c *gin.Context, ap store.Approval, who, onBehalf, decision, reason string) (bool, int, error) {
	now := time.Now().UTC()
	d := store.ApprovalDecision{ID: uuid.NewString(), OrgID: ap.OrgID, ApprovalID: ap.ID, Decider: who, Decision: decision, Signature: decisionSignature(who, now, ap.PayloadHash, decision), CreatedAt: now}
	if onBehalf != "" {
		d.OnBehalf = &onBehalf
	}
	if reason != "" {
		d.Reason = &reason
	}
	if err := s.DB.WithContext(c).Create(&d).Error; err != nil {
		return false, 0, err
	}
	s.audit(c, ap.OrgID, "approval.vote", "approval:"+ap.ID, "success", nil, gin.H{"decision": decision, "signature": d.Signature},
		map[string]any{"decider": who, "on_behalf": onBehalf, "quorum": ap.Quorum})
	if decision == "reject" {
		return true, 0, nil
	}
	var n int64
	s.DB.WithContext(c).Model(&store.ApprovalDecision{}).Where("approval_id = ? AND decision IN ?", ap.ID, []string{"approve", "edit"}).Count(&n)
	return int(n) >= max(1, ap.Quorum), int(n), nil
}

func (s *Server) putDelegation(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	var b struct {
		Principal string    `json:"principal"`
		Delegate  string    `json:"delegate"`
		Until     time.Time `json:"until"`
	}
	if err := c.ShouldBindJSON(&b); err != nil || b.Delegate == "" || b.Until.Before(time.Now()) {
		fail(c, http.StatusBadRequest, "bad_request", "delegate·until(미래) 필수", nil)
		return
	}
	who := firstNonEmpty(principal(c).Sub, "anonymous")
	b.Principal = firstNonEmpty(b.Principal, who)
	if b.Principal != who && !s.can(c, "admin", orgObj(org)) {
		fail(c, http.StatusForbidden, "forbidden", "자기 위임만 (관리자는 대신 설정 가능)", nil)
		return
	}
	s.tenantScope(c, org)
	s.db(c).Save(&store.ApprovalDelegation{OrgID: org, Principal: b.Principal, Delegate: b.Delegate, Until: b.Until.UTC(), CreatedAt: time.Now().UTC()})
	s.audit(c, org, "approval.delegate", "delegation:"+b.Principal, "success", nil, gin.H{"delegate": b.Delegate, "until": b.Until}, nil)
	c.JSON(http.StatusOK, gin.H{"org": org, "principal": b.Principal, "delegate": b.Delegate, "until": b.Until.UTC()})
}

// ---- SLA·지표 ----

// EscalateApprovals 는 SLA 를 넘긴 대기 요청을 1회 에스컬레이션한다 (주기 실행, X-HITL-05).
func (s *Server) EscalateApprovals(ctx context.Context) {
	var items []store.Approval
	db := s.DB.WithContext(ctx)
	db.Where("status = ? AND sla_seconds > 0 AND escalated_at IS NULL", "pending").Find(&items)
	now := time.Now().UTC()
	for _, ap := range items {
		if now.Sub(ap.CreatedAt) < time.Duration(ap.SlaSeconds)*time.Second {
			continue
		}
		db.Model(&store.Approval{}).Where("id = ? AND escalated_at IS NULL", ap.ID).Update("escalated_at", now)
		var to []string
		if ap.EscalateTo != nil {
			_ = json.Unmarshal([]byte(*ap.EscalateTo), &to)
		}
		ev := gin.H{"org": ap.OrgID, "id": ap.ID, "action": ap.Action, "risk": ap.RiskLevel, "escalateTo": to, "waitedSeconds": int(now.Sub(ap.CreatedAt).Seconds()),
			"slaSeconds": ap.SlaSeconds}
		s.emit(ctx, "plt.approval.escalated", ev)
	}
}

func (s *Server) approvalStats(c *gin.Context) {
	org := orgOf(c)
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	var items []store.Approval
	s.db(c).Where("org_id = ?", org).Find(&items)
	by := map[string]int{}
	lat := []float64{}
	esc := 0
	for _, a := range items {
		by[a.Status]++
		if a.DecidedAt != nil && (a.Status == "approved" || a.Status == "edited" || a.Status == "rejected") {
			lat = append(lat, a.DecidedAt.Sub(a.CreatedAt).Seconds())
		}
		if a.EscalatedAt != nil {
			esc++
		}
	}
	sort.Float64s(lat)
	pct := func(q float64) any {
		if len(lat) == 0 {
			return nil
		}
		return lat[min(len(lat)-1, int(q*float64(len(lat))))]
	}
	c.JSON(http.StatusOK, gin.H{"org": org, "byStatus": by, "escalated": esc, "decided": len(lat), "hitl_approval_latency_s": gin.H{"p50": pct(0.5), "p95": pct(0.95)}})
}
