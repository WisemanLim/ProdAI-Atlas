package api

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sort"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"

	"atlas-manager/internal/iam"
	"atlas-manager/internal/store"
)

// 통합 승인함 (FR-X-HITL-01·02·07): 에이전트가 고위험 행동 직전 승인 요청을 보내고 멈춘다 →
// 승인자가 위험도·만료순으로 보고 승인/반려/수정 후 승인 → Manager 가 같은 A2A Task 에 결정을 보내 재개 → 결정은 해시 체인 감사.

var riskOrder = map[string]int{"critical": 0, "high": 1, "medium": 2, "low": 3}

type approvalRequest struct {
	Source     string         `json:"source"`
	SourceRef  string         `json:"sourceRef"` // 루프 = A2A taskId
	AgentID    string         `json:"agentId"`
	UnitID     string         `json:"unitId"`
	Action     string         `json:"action"`
	RiskLevel  string         `json:"riskLevel"`
	Payload    map[string]any `json:"payload"`
	TraceID    string         `json:"traceId"`
	ExpiresInS int            `json:"expiresInS"`
	Project    string         `json:"project"`
}

func payloadHash(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

// createApproval: 에이전트 주체(자기 토큰) 또는 invoke 권한자가 요청한다.
func (s *Server) createApproval(c *gin.Context) {
	var r approvalRequest
	if err := c.ShouldBindJSON(&r); err != nil || r.Action == "" || r.SourceRef == "" || r.AgentID == "" {
		fail(c, http.StatusBadRequest, "bad_request", "action·sourceRef·agentId 필수", nil)
		return
	}
	if _, ok := riskOrder[r.RiskLevel]; !ok {
		r.RiskLevel = "high"
	}
	org := orgOf(c)
	var a store.Agent
	if err := s.DB.WithContext(c).First(&a, "id = ? AND org_id = ?", r.AgentID, org).Error; err != nil {
		fail(c, http.StatusNotFound, "not_found", "에이전트 없음", nil)
		return
	}
	if p := principal(c); p.Kind == "agent" && p.Sub != "agent:"+a.ID {
		fail(c, http.StatusForbidden, "forbidden", "에이전트는 자기 이름으로만 승인 요청", nil)
		return
	} else if p.Kind != "agent" && !s.require(c, "invoke", projObj(org, a.Project)) {
		return
	}
	body, _ := json.Marshal(r.Payload)
	ttl := time.Duration(max(60, min(r.ExpiresInS, 7*86400))) * time.Second
	if r.ExpiresInS == 0 {
		ttl = 24 * time.Hour
	}
	now := time.Now().UTC()
	ap := store.Approval{ID: iam.ULID(), OrgID: org, Project: firstNonEmpty(r.Project, a.Project), Source: firstNonEmpty(r.Source, "loop"), SourceRef: r.SourceRef,
		AgentID: a.ID, UnitID: firstNonEmpty(r.UnitID, a.UnitID), Action: r.Action, RiskLevel: r.RiskLevel, Payload: string(body), PayloadHash: payloadHash(body),
		TraceID: r.TraceID, Requester: firstNonEmpty(principal(c).Sub, "anonymous"), Status: "pending", ExpiresAt: now.Add(ttl), CreatedAt: now}
	if code, ecode, msg := s.applyApprovalPolicy(s.db(c), &ap, r.Payload); code != 0 { // 정책·미리보기·라우팅 (X-HITL-03·04 · GR-POL-06)
		s.audit(c, org, "approval.request", "agent:"+a.ID, "denied", nil, nil, map[string]any{"action": ap.Action, "reason": ecode})
		fail(c, code, ecode, msg, nil)
		return
	}
	if err := s.DB.WithContext(c).Create(&ap).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	s.audit(c, org, "approval.request", "approval:"+ap.ID, "success", nil, gin.H{"payload_hash": ap.PayloadHash}, map[string]any{"action": ap.Action, "risk": ap.RiskLevel, "agent": a.Name, "payload_hash": ap.PayloadHash})
	s.emit(c, "plt.approval.requested", gin.H{"org": org, "id": ap.ID, "action": ap.Action, "risk": ap.RiskLevel, "agent": a.Name, "expiresAt": ap.ExpiresAt})
	c.JSON(http.StatusCreated, ap)
}

func (s *Server) listApprovals(c *gin.Context) {
	org := orgOf(c)
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	q := s.db(c).Where("org_id = ?", org)
	for k, col := range map[string]string{"status": "status", "risk": "risk_level", "unit": "unit_id", "agent": "agent_id", "requester": "requester"} {
		if v := c.Query(k); v != "" {
			q = q.Where(col+" = ?", v)
		}
	}
	var items []store.Approval
	if err := q.Limit(500).Find(&items).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	sort.SliceStable(items, func(i, j int) bool { // 위험도 → 만료 임박순 (HITL-02)
		if riskOrder[items[i].RiskLevel] != riskOrder[items[j].RiskLevel] {
			return riskOrder[items[i].RiskLevel] < riskOrder[items[j].RiskLevel]
		}
		return items[i].ExpiresAt.Before(items[j].ExpiresAt)
	})
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func (s *Server) getApproval(c *gin.Context) {
	org := orgOf(c)
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	var ap store.Approval
	if err := s.DB.WithContext(c).First(&ap, "id = ? AND org_id = ?", c.Param("id"), org).Error; err != nil {
		fail(c, http.StatusNotFound, "not_found", "승인 요청 없음", nil)
		return
	}
	c.JSON(http.StatusOK, gin.H{"approval": ap, "traceUrl": "/svc/ob/v1/traces/" + ap.TraceID})
}

// decideApproval: approve · reject · edit(수정 후 승인 — payload.arguments 교체). 승인자 = 프로젝트 operator 이상, 요청자 본인 불가.
func (s *Server) decideApproval(c *gin.Context) {
	org := orgOf(c)
	var r struct {
		Decision string         `json:"decision"`
		Reason   string         `json:"reason"`
		Payload  map[string]any `json:"payload"`
	}
	if err := c.ShouldBindJSON(&r); err != nil || (r.Decision != "approve" && r.Decision != "reject" && r.Decision != "edit") {
		fail(c, http.StatusBadRequest, "bad_request", "decision = approve | reject | edit", nil)
		return
	}
	var ap store.Approval
	if err := s.DB.WithContext(c).First(&ap, "id = ? AND org_id = ?", c.Param("id"), org).Error; err != nil {
		fail(c, http.StatusNotFound, "not_found", "승인 요청 없음", nil)
		return
	}
	if !s.require(c, "operator", projObj(org, ap.Project)) {
		return
	}
	p := principal(c)
	who := firstNonEmpty(p.Sub, "anonymous")
	if s.authOn() && who == ap.Requester {
		fail(c, http.StatusForbidden, "self_approval", "요청자 본인은 승인할 수 없음", nil)
		return
	}
	if ap.Status != "pending" {
		fail(c, http.StatusConflict, "already_decided", "이미 "+ap.Status, nil)
		return
	}
	if time.Now().After(ap.ExpiresAt) {
		fail(c, http.StatusConflict, "expired", "만료된 요청", nil)
		return
	}
	ok, onBehalf := s.eligible(c, ap, who)
	if !ok {
		fail(c, http.StatusForbidden, "not_an_approver", "이 요청의 승인자(또는 대리인)가 아님", gin.H{"policyRule": ap.PolicyRule})
		return
	}
	done, votes, err := s.recordDecision(c, ap, who, onBehalf, r.Decision, r.Reason)
	if err != nil {
		fail(c, http.StatusConflict, "already_voted", "이미 결정함 (승인자 1인 1표)", nil)
		return
	}
	if !done { // 다단 승인 — quorum 미달 (X-HITL-04)
		c.JSON(http.StatusAccepted, gin.H{"id": ap.ID, "status": "pending", "approvals": votes, "quorum": ap.Quorum})
		return
	}
	now := time.Now().UTC()
	status := map[string]string{"approve": "approved", "reject": "rejected", "edit": "edited"}[r.Decision]
	upd := map[string]any{"status": status, "decision_by": who, "decision_reason": r.Reason, "decided_at": now}
	var edited []byte
	if r.Decision == "edit" {
		edited, _ = json.Marshal(r.Payload)
		upd["edited_payload"] = string(edited)
	}
	// 낙관적 잠금: pending 일 때만 바꾼다 (동시 결정 방지)
	res := s.DB.WithContext(c).Model(&store.Approval{}).Where("id = ? AND status = ?", ap.ID, "pending").Updates(upd)
	if res.Error != nil || res.RowsAffected == 0 {
		fail(c, http.StatusConflict, "already_decided", "다른 결정이 먼저 반영됨", nil)
		return
	}
	s.audit(c, org, "approval.decide", "approval:"+ap.ID, "success", gin.H{"status": "pending", "payload_hash": ap.PayloadHash},
		gin.H{"status": status, "edited_hash": map[bool]string{true: payloadHash(edited), false: ""}[len(edited) > 0]},
		map[string]any{"decision": r.Decision, "reason": r.Reason, "action": ap.Action, "payload_hash": ap.PayloadHash})
	cb := s.resumeSource(c, ap, r.Decision, r.Payload)
	s.DB.WithContext(c).Model(&store.Approval{}).Where("id = ?", ap.ID).Update("callback_result", cb)
	s.emit(c, "plt.approval.decided", gin.H{"org": org, "id": ap.ID, "decision": status, "by": who, "action": ap.Action})
	if r.Decision == "reject" && r.Reason != "" { // 반려 사유는 골든셋 후보 (HITL-07 → EV-GOLD)
		s.emit(c, "plt.approval.rejected", gin.H{"org": org, "id": ap.ID, "reason": r.Reason, "traceId": ap.TraceID, "evGoldCandidate": true})
	}
	ap.Status, ap.CallbackResult = status, &cb
	c.JSON(http.StatusOK, ap)
}

// resumeSource 는 결정을 요청한 에이전트의 같은 A2A Task 에 보낸다 (루프 HITL 재개).
func (s *Server) resumeSource(ctx context.Context, ap store.Approval, decision string, edited map[string]any) string {
	if ap.Source == "deploy" && ap.Action == "deploy.promote" { // 환경 승격 승인 (FR-PLT-DEP-08)
		if decision != "approve" && decision != "edit" {
			return "promotion_rejected"
		}
		var p map[string]any
		_ = json.Unmarshal([]byte(ap.Payload), &p)
		if d, err := s.applyPromotion(ctx, p); err != nil {
			return "promotion_error"
		} else {
			return "promoted:" + d.ID
		}
	}
	if ap.Source != "loop" && ap.Source != "a2a" {
		return "no_callback"
	}
	data := map[string]any{"approval": map[string]string{"approve": "approve", "edit": "approve", "reject": "deny", "expire": "deny"}[decision]}
	if decision == "edit" {
		if args, ok := edited["arguments"]; ok {
			data["edited_arguments"] = args
		}
	}
	body, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": uuid.NewString(), "method": "SendMessage", "params": map[string]any{
		"tenant": ap.OrgID, "message": map[string]any{"role": "ROLE_USER", "messageId": uuid.NewString(), "taskId": ap.SourceRef,
			"parts": []map[string]any{{"data": data}}}, "configuration": map[string]any{"returnImmediately": true}}})
	out, err := s.callAgent(ctx, ap.AgentID, ap.OrgID, "dev", "approval:"+ap.ID, body)
	if err != nil {
		slog.Warn("approval callback failed", "approval", ap.ID, "err", err)
		return "callback_error"
	}
	var rpc struct {
		Result struct {
			Task struct {
				Status struct{ State string } `json:"status"`
			} `json:"task"`
		} `json:"result"`
		Error *struct{ Message string } `json:"error"`
	}
	_ = json.Unmarshal(out, &rpc)
	if rpc.Error != nil {
		return "rpc_error"
	}
	return firstNonEmpty(rpc.Result.Task.Status.State, "sent")
}

// callAgent 는 Manager 자신이 에이전트를 부른다(테넌트 JWT 부착) — agent-proxy 와 같은 대상 해석.
func (s *Server) callAgent(ctx context.Context, agentID, org, env, sub string, body []byte) ([]byte, error) {
	var a store.Agent
	if err := s.DB.WithContext(ctx).First(&a, "id = ? AND org_id = ?", agentID, org).Error; err != nil {
		return nil, err
	}
	d, err := pickDeployment(s.DB.WithContext(ctx), agentID, env, sub)
	if err != nil {
		return nil, fmt.Errorf("ready 배포 없음")
	}
	target := d.Endpoint + "/a2a"
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, target, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if s.Signer != nil {
		if tok, err := s.Signer.Mint(sub, "agent:"+agentID, org, a.Project, env, 5*time.Minute); err == nil {
			req.Header.Set("Authorization", "Bearer "+tok)
		}
	}
	res, err := s.Proxy.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	return io.ReadAll(io.LimitReader(res.Body, 1<<20))
}

// ExpireApprovals 는 만료된 pending 을 expired 로 바꾸고 요청 에이전트에 deny 를 보낸다(주기 실행).
func (s *Server) ExpireApprovals(ctx context.Context) {
	var due []store.Approval
	s.DB.WithContext(ctx).Where("status = ? AND expires_at < ?", "pending", time.Now().UTC()).Limit(100).Find(&due)
	for _, ap := range due {
		res := s.DB.WithContext(ctx).Model(&store.Approval{}).Where("id = ? AND status = ?", ap.ID, "pending").
			Updates(map[string]any{"status": "expired", "decided_at": time.Now().UTC()})
		if res.RowsAffected == 0 {
			continue
		}
		cb := s.resumeSource(ctx, ap, "expire", nil)
		s.DB.WithContext(ctx).Model(&store.Approval{}).Where("id = ?", ap.ID).Update("callback_result", cb)
		if s.Audit != nil {
			_, _ = s.Audit.Append(ctx, iam.Entry{Org: ap.OrgID, ActorType: "system", ActorID: "approval-expirer", Action: "approval.expire",
				Resource: "approval:" + ap.ID, Result: "success", Before: gin.H{"status": "pending"}, After: gin.H{"status": "expired"}})
		}
		s.emit(ctx, "plt.approval.expired", gin.H{"org": ap.OrgID, "id": ap.ID, "action": ap.Action})
	}
}

// withdrawApprovals: 요청 에이전트가 같은 Task 의 대기 요청을 거둔다 — 승인함 밖(같은 Task 직접 응답)에서 이미 재개된 경우.
func (s *Server) withdrawApprovals(c *gin.Context) {
	var r struct {
		SourceRef string `json:"sourceRef"`
		AgentID   string `json:"agentId"`
	}
	if err := c.ShouldBindJSON(&r); err != nil || r.SourceRef == "" || r.AgentID == "" {
		fail(c, http.StatusBadRequest, "bad_request", "sourceRef·agentId 필수", nil)
		return
	}
	org := orgOf(c)
	var a store.Agent
	if err := s.DB.WithContext(c).First(&a, "id = ? AND org_id = ?", r.AgentID, org).Error; err != nil {
		fail(c, http.StatusNotFound, "not_found", "에이전트 없음", nil)
		return
	}
	if p := principal(c); p.Kind == "agent" && p.Sub != "agent:"+a.ID {
		fail(c, http.StatusForbidden, "forbidden", "자기 요청만 거둘 수 있음", nil)
		return
	} else if p.Kind != "agent" && !s.require(c, "invoke", projObj(org, a.Project)) {
		return
	}
	var ids []string
	s.DB.WithContext(c).Model(&store.Approval{}).Where("org_id = ? AND agent_id = ? AND source_ref = ? AND status = ?", org, a.ID, r.SourceRef, "pending").Pluck("id", &ids)
	if len(ids) > 0 {
		s.DB.WithContext(c).Model(&store.Approval{}).Where("id IN ? AND status = ?", ids, "pending").
			Updates(map[string]any{"status": "withdrawn", "decided_at": time.Now().UTC(), "callback_result": "resolved_in_task"})
		for _, id := range ids {
			s.audit(c, org, "approval.withdraw", "approval:"+id, "success", gin.H{"status": "pending"}, gin.H{"status": "withdrawn"}, map[string]any{"sourceRef": r.SourceRef})
		}
	}
	c.JSON(http.StatusOK, gin.H{"withdrawn": ids})
}
