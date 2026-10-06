package api

// 배포 고도화 (FR-PLT-DEP-05·06·07·08·09·10 · PLT-MGR-07·09 · PLT-DEP-03):
//   - 슬롯: 환경마다 primary 1 + canary 1 + standby 1. agent-proxy 는 primary·canary 를 가중치로 고르고(고정 배정 키 해시), standby 는 라우팅하지 않는다.
//   - 롤아웃: canary(가중치 단계 + 평가 게이트 + 오류율 자동 롤백) · bluegreen(0 → 100 전환, 이전 primary 는 standby 로 보존).
//   - 롤백: standby 가 있으면 즉시 맞바꿈, 없으면 이전 리비전 기록으로 되돌림(재기동은 드라이버 몫).
//   - 승격: dev → staging → prod. 정책이 requireApproval 이면 승인함(source=deploy)을 거친다.
//   - 정책: 환경별 digest 고정·허용 레지스트리·매니페스트 서명(ed25519)·승인 필요.
//   - 드리프트: 실제 Agent Card(version·skills) vs 배포 리비전 매니페스트 (prober).
//   - 스케일: 선언 복제 수(compose 는 기록만, kubernetes 매니페스트에 반영) · 토폴로지: 관찰된 A2A 호출 간선.

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gopkg.in/yaml.v3"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"atlas-manager/internal/iam"
	"atlas-manager/internal/manifest"
	"atlas-manager/internal/store"
)

// ---- 라우팅 ----

// pickDeployment 는 ready 인 primary·canary 중 하나를 고른다. key 가 있으면 같은 키는 같은 쪽(고정 배정).
func pickDeployment(db *gorm.DB, agentID, env, key string) (store.Deployment, error) {
	var deps []store.Deployment
	if err := db.Where("agent_id = ? AND env = ? AND status = ? AND slot IN ?", agentID, env, store.DeployReady,
		[]string{store.SlotPrimary, store.SlotCanary}).Order("updated_at DESC").Find(&deps).Error; err != nil {
		return store.Deployment{}, err
	}
	var primary, canary *store.Deployment
	for i := range deps {
		switch deps[i].Slot {
		case store.SlotPrimary:
			if primary == nil {
				primary = &deps[i]
			}
		case store.SlotCanary:
			if canary == nil {
				canary = &deps[i]
			}
		}
	}
	switch {
	case primary == nil && canary == nil:
		return store.Deployment{}, gorm.ErrRecordNotFound
	case canary == nil || canary.Weight <= 0:
		if primary == nil {
			return store.Deployment{}, gorm.ErrRecordNotFound
		}
		return *primary, nil
	case primary == nil || canary.Weight >= 100:
		return *canary, nil
	}
	if bucket(key) < canary.Weight {
		return *canary, nil
	}
	return *primary, nil
}

func bucket(key string) int {
	if key == "" {
		return rand.IntN(100)
	}
	h := sha256.Sum256([]byte(key))
	return int(binary.BigEndian.Uint32(h[:4]) % 100)
}

// recordCall 은 배포 호출·오류 수와 토폴로지 간선을 쌓는다 (롤아웃 오류율 게이트·FR-PLT-MGR-09).
func (s *Server) recordCall(ctx context.Context, d store.Deployment, caller string, failed bool) {
	e := 0
	if failed {
		e = 1
	}
	db := s.DB.WithContext(ctx)
	db.Model(&store.Deployment{}).Where("id = ?", d.ID).Updates(map[string]any{"calls": gorm.Expr("calls + 1"), "errors": gorm.Expr("errors + ?", e)})
	db.Clauses(clause.OnConflict{Columns: []clause.Column{{Name: "org_id"}, {Name: "env"}, {Name: "caller"}, {Name: "callee"}},
		DoUpdates: clause.Assignments(map[string]any{"calls": gorm.Expr("topology_edges.calls + 1"), "errors": gorm.Expr("topology_edges.errors + ?", e),
			"last_at": time.Now().UTC()})}).
		Create(&store.TopologyEdge{OrgID: d.OrgID, Env: d.Env, Caller: caller, Callee: d.AgentID, Calls: 1, Errors: e, LastAt: time.Now().UTC()})
}

// ---- 배포 정책 (DEP-10) ----

type deployPolicy struct {
	RequireDigest     bool              `json:"requireDigest"`
	AllowedRegistries []string          `json:"allowedRegistries,omitempty"`
	RequireSignature  bool              `json:"requireSignature"`
	TrustedKeys       map[string]string `json:"trustedKeys,omitempty"` // kid → ed25519 공개키 PEM
	RequireApproval   bool              `json:"requireApproval"`
}

func (s *Server) policyFor(db *gorm.DB, org, env string) (deployPolicy, bool) {
	var row store.DeployPolicy
	var p deployPolicy
	if db.Where("org_id = ? AND env = ?", org, env).Limit(1).Find(&row).RowsAffected == 0 { // Find: 정책 없음은 정상(로그 소음 없음)
		return p, false
	}
	_ = json.Unmarshal([]byte(row.Policy), &p)
	return p, true
}

var digestRe = regexp.MustCompile(`@sha256:[0-9a-f]{64}$`)

// SignedManifestMessage 는 매니페스트 서명 대상이다: "atlas-manifest/v1|<spec 해시>".
func SignedManifestMessage(specHash string) []byte { return []byte("atlas-manifest/v1|" + specHash) }

// checkPolicy 는 배포할 리비전이 환경 정책을 지키는지 본다. 위반 사유 목록(비면 통과).
func checkPolicy(p deployPolicy, rev store.Revision) []string {
	var raw map[string]any
	_ = json.Unmarshal([]byte(rev.Manifest), &raw)
	spec, _ := raw["spec"].(map[string]any)
	art, _ := spec["artifacts"].(map[string]any)
	image, _ := art["image"].(string)
	var out []string
	if p.RequireDigest && !digestRe.MatchString(image) {
		out = append(out, "spec.artifacts.image 이 digest(@sha256:…) 로 고정되지 않음")
	}
	if len(p.AllowedRegistries) > 0 {
		ok := false
		for _, r := range p.AllowedRegistries {
			if image != "" && strings.HasPrefix(image, strings.TrimRight(r, "/")+"/") {
				ok = true
			}
		}
		if !ok {
			out = append(out, fmt.Sprintf("이미지 %q 가 허용 레지스트리 %v 밖", image, p.AllowedRegistries))
		}
	}
	if p.RequireSignature {
		sig, _ := art["signature"].(map[string]any)
		kid, _ := sig["kid"].(string)
		sv, _ := sig["sig"].(string)
		pub, okKey := p.TrustedKeys[kid]
		switch {
		case sig == nil:
			out = append(out, "매니페스트 서명 없음 (spec.artifacts.signature)")
		case !okKey:
			out = append(out, "신뢰 목록에 없는 서명 키 "+kid)
		default:
			if !verifyEd25519(pub, SignedManifestMessage(unsignedSpecHash(raw)), sv) {
				out = append(out, "매니페스트 서명 불일치")
			}
		}
	}
	return out
}

// unsignedSpecHash 는 서명 대상 해시다: {name, unitId, spec − artifacts.signature}. Manager 가 채우는 metadata(layer·concept)는 빼서
// 서명자가 보낸 그대로 검증된다(서명이 자기 자신을 덮지 않게 signature 도 뺀다).
func unsignedSpecHash(raw map[string]any) string {
	b, _ := json.Marshal(raw)
	var cp map[string]any
	_ = json.Unmarshal(b, &cp)
	spec, _ := cp["spec"].(map[string]any)
	if art, ok := spec["artifacts"].(map[string]any); ok {
		delete(art, "signature")
	}
	md, _ := cp["metadata"].(map[string]any)
	c, _ := json.Marshal(map[string]any{"name": md["name"], "unitId": md["unitId"], "spec": spec}) // encoding/json 은 맵 키를 정렬 → 결정적
	h := sha256.Sum256(c)
	return fmt.Sprintf("%x", h[:])
}

// manifestDigest: POST /api/v1/manifests/digest — 서명 도구가 쓸 정확한 서명 메시지(언어별 JSON 직렬화 차이 회피).
func (s *Server) manifestDigest(c *gin.Context) {
	var raw map[string]any
	if err := c.ShouldBindJSON(&raw); err != nil {
		fail(c, http.StatusBadRequest, "bad_request", err.Error(), nil)
		return
	}
	h := unsignedSpecHash(raw)
	c.JSON(http.StatusOK, gin.H{"specHash": h, "message": string(SignedManifestMessage(h)), "alg": "ed25519",
		"place": "spec.artifacts.signature = {kid, sig: base64(ed25519(message))}"})
}

func verifyEd25519(pubPEM string, msg []byte, sigB64 string) bool {
	blk, _ := pem.Decode([]byte(pubPEM))
	if blk == nil {
		return false
	}
	k, err := x509.ParsePKIXPublicKey(blk.Bytes)
	if err != nil {
		return false
	}
	pk, ok := k.(ed25519.PublicKey)
	sig, err := base64.StdEncoding.DecodeString(sigB64)
	return ok && err == nil && ed25519.Verify(pk, msg, sig)
}

func (s *Server) getDeployPolicy(c *gin.Context) {
	org, env := c.Param("org"), c.Param("env")
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	p, ok := s.policyFor(s.db(c), org, env)
	c.JSON(http.StatusOK, gin.H{"org": org, "env": env, "policy": p, "configured": ok})
}

func (s *Server) putDeployPolicy(c *gin.Context) {
	org, env := c.Param("org"), c.Param("env")
	if _, ok := s.orgGuard(c, org); !ok || !s.require(c, "admin", orgObj(org)) {
		return
	}
	var p deployPolicy
	if err := c.ShouldBindJSON(&p); err != nil {
		fail(c, http.StatusBadRequest, "bad_request", err.Error(), nil)
		return
	}
	b, _ := json.Marshal(p)
	s.tenantScope(c, org)
	if err := s.db(c).Save(&store.DeployPolicy{OrgID: org, Env: env, Policy: string(b), UpdatedAt: time.Now().UTC()}).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	s.audit(c, org, "deploy.policy.update", "deploy_policy:"+env, "success", nil, gin.H{"policy": p}, nil)
	c.JSON(http.StatusOK, gin.H{"org": org, "env": env, "policy": p})
}

// enforcePolicy 는 위반이면 422 + 감사(deploy.policy_denied) 후 false.
func (s *Server) enforcePolicy(c *gin.Context, org, env string, rev store.Revision, a store.Agent) bool {
	p, ok := s.policyFor(s.db(c), org, env)
	if !ok {
		return true
	}
	if v := checkPolicy(p, rev); len(v) > 0 {
		s.audit(c, org, "deploy.policy_denied", "agent:"+a.ID, "denied", nil, nil, map[string]any{"env": env, "rev": rev.Rev, "violations": v})
		s.emit(c, "plt.deploy.policy_denied", gin.H{"org": org, "agentId": a.ID, "env": env, "rev": rev.Rev, "violations": v})
		fail(c, http.StatusUnprocessableEntity, "policy_violation", "배포 정책 위반", gin.H{"violations": v})
		return false
	}
	return true
}

// ---- 롤아웃 (DEP-05·06) ----

type rolloutRequest struct {
	Env        string         `json:"env"`
	Strategy   string         `json:"strategy"` // canary | bluegreen
	Rev        int            `json:"rev"`      // 0 = 에이전트 현재 리비전
	Endpoint   string         `json:"endpoint"`
	Driver     string         `json:"driver"`
	Steps      []int          `json:"steps"` // canary 가중치 단계 (기본 10·50·100)
	Gate       rolloutGate    `json:"gate"`
	AssignKeys []string       `json:"-"`
	Extra      map[string]any `json:"-"`
}

type rolloutGate struct {
	MinCalls     int            `json:"minCalls"`     // 오류율 판정 최소 호출 수 (기본 5)
	MaxErrorRate float64        `json:"maxErrorRate"` // 초과 시 자동 롤백 (기본 0.2)
	Suite        map[string]any `json:"suite,omitempty"`
	AutoRollback *bool          `json:"autoRollback,omitempty"`
}

type rolloutView struct {
	store.Rollout
	Steps   []int            `json:"steps"`
	Weight  int              `json:"weight"`
	Gate    rolloutGate      `json:"gate"`
	History []map[string]any `json:"history"`
	Canary  map[string]any   `json:"canary,omitempty"`
}

func (s *Server) rolloutView(db *gorm.DB, r store.Rollout) rolloutView {
	v := rolloutView{Rollout: r}
	_ = json.Unmarshal([]byte(r.Steps), &v.Steps)
	_ = json.Unmarshal([]byte(r.Gate), &v.Gate)
	_ = json.Unmarshal([]byte(r.History), &v.History)
	var d store.Deployment
	if db.First(&d, "id = ?", r.ToDeploymentID).Error == nil {
		v.Weight = d.Weight
		v.Canary = map[string]any{"id": d.ID, "status": d.Status, "slot": d.Slot, "calls": d.Calls, "errors": d.Errors, "endpoint": d.Endpoint}
	}
	return v
}

func appendHistory(r *store.Rollout, ev map[string]any) {
	var h []map[string]any
	_ = json.Unmarshal([]byte(r.History), &h)
	ev["at"] = time.Now().UTC().Format(time.RFC3339)
	b, _ := json.Marshal(append(h, ev))
	r.History = string(b)
}

func (s *Server) createRollout(c *gin.Context) {
	a, ok := s.findAgent(c, "viewer")
	if !ok {
		return
	}
	var r rolloutRequest
	if err := c.ShouldBindJSON(&r); err != nil {
		fail(c, http.StatusBadRequest, "bad_request", err.Error(), nil)
		return
	}
	r.Env, r.Driver = firstNonEmpty(r.Env, "dev"), firstNonEmpty(r.Driver, "compose")
	if r.Strategy == "" {
		r.Strategy = "canary"
	}
	if r.Strategy != "canary" && r.Strategy != "bluegreen" {
		fail(c, http.StatusUnprocessableEntity, "invalid_rollout", "strategy = canary | bluegreen", nil)
		return
	}
	if r.Strategy == "bluegreen" {
		r.Steps = []int{0, 100}
	} else if len(r.Steps) == 0 {
		r.Steps = []int{10, 50, 100}
	}
	for i, w := range r.Steps {
		if w < 0 || w > 100 || (i > 0 && w < r.Steps[i-1]) {
			fail(c, http.StatusUnprocessableEntity, "invalid_rollout", "steps 는 0~100 오름차순", gin.H{"steps": r.Steps})
			return
		}
	}
	if r.Steps[len(r.Steps)-1] != 100 {
		r.Steps = append(r.Steps, 100)
	}
	org := a.OrgID
	if _, ok := s.orgGuard(c, org); !ok || !s.require(c, "deploy", envObj(org, a.Project, r.Env)) {
		return
	}
	if !validEndpoint(r.Driver, r.Endpoint) {
		fail(c, http.StatusUnprocessableEntity, "invalid_deployment", "driver(local|compose|kubernetes)·endpoint 확인", nil)
		return
	}
	db := s.db(c)
	rev := firstNonZero(r.Rev, a.CurrentRev)
	var revRow store.Revision
	if db.First(&revRow, "agent_id = ? AND rev = ?", a.ID, rev).Error != nil {
		fail(c, http.StatusNotFound, "not_found", fmt.Sprintf("리비전 %d 없음", rev), nil)
		return
	}
	if !s.enforcePolicy(c, org, r.Env, revRow, a) {
		return
	}
	var running int64
	db.Model(&store.Rollout{}).Where("agent_id = ? AND env = ? AND status = ?", a.ID, r.Env, "running").Count(&running)
	if running > 0 {
		fail(c, http.StatusConflict, "rollout_in_progress", "같은 환경에 진행 중인 롤아웃이 있음", nil)
		return
	}
	var primary store.Deployment
	hasPrimary := db.Where("agent_id = ? AND env = ? AND slot = ? AND status <> ?", a.ID, r.Env, store.SlotPrimary, store.DeployStopped).First(&primary).Error == nil
	now := time.Now().UTC()
	ro := store.Rollout{ID: uuid.NewString(), OrgID: org, AgentID: a.ID, Env: r.Env, Strategy: r.Strategy, Rev: rev, Status: "running",
		CreatedBy: firstNonEmpty(principal(c).Sub, "anonymous"), CreatedAt: now, UpdatedAt: now, History: "[]"}
	if hasPrimary {
		ro.FromDeploymentID = &primary.ID
	}
	weight := r.Steps[0]
	if !hasPrimary {
		weight = 100 // 기존 primary 가 없으면 첫 배포 — 단계 없이 받는다
	}
	g := r.Gate
	if g.MinCalls == 0 {
		g.MinCalls = 5
	}
	if g.MaxErrorRate == 0 {
		g.MaxErrorRate = 0.2
	}
	sb, _ := json.Marshal(r.Steps)
	gb, _ := json.Marshal(g)
	ro.Steps, ro.Gate = string(sb), string(gb)
	d := store.Deployment{ID: uuid.NewString(), OrgID: org, AgentID: a.ID, Rev: rev, Env: r.Env, Driver: r.Driver, Endpoint: strings.TrimRight(r.Endpoint, "/"),
		Status: store.DeployPending, Slot: store.SlotCanary, Weight: weight, Replicas: 1, RolloutID: &ro.ID, CreatedAt: now, UpdatedAt: now}
	ro.ToDeploymentID = d.ID
	appendHistory(&ro, map[string]any{"event": "started", "weight": weight, "rev": rev})
	err := db.Transaction(func(tx *gorm.DB) error {
		// 남은 canary 는 정리 (이전 롤아웃이 비정상 종료됐을 때)
		tx.Model(&store.Deployment{}).Where("agent_id = ? AND env = ? AND slot = ? AND status <> ?", a.ID, r.Env, store.SlotCanary, store.DeployStopped).
			Updates(map[string]any{"status": store.DeployStopped, "updated_at": now})
		if err := tx.Create(&d).Error; err != nil {
			return err
		}
		return tx.Create(&ro).Error
	})
	if err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	s.audit(c, org, "deploy.rollout.start", "rollout:"+ro.ID, "success", nil, gin.H{"agent": a.Name, "env": r.Env, "rev": rev, "strategy": r.Strategy, "steps": r.Steps}, nil)
	s.emit(c, "plt.deploy.rollout.started", gin.H{"org": org, "id": ro.ID, "agentId": a.ID, "env": r.Env, "strategy": r.Strategy, "weight": weight})
	c.JSON(http.StatusCreated, s.rolloutView(db, ro))
}

func firstNonZero(v ...int) int {
	for _, x := range v {
		if x != 0 {
			return x
		}
	}
	return 0
}

func validEndpoint(driver, endpoint string) bool {
	if !drivers[driver] {
		return false
	}
	return strings.HasPrefix(endpoint, "http://") || strings.HasPrefix(endpoint, "https://")
}

func (s *Server) findRollout(c *gin.Context) (store.Rollout, store.Agent, bool) {
	var ro store.Rollout
	var a store.Agent
	org := orgOf(c)
	s.tenantScope(c, org)
	if s.db(c).First(&ro, "id = ? AND org_id = ?", c.Param("rid"), org).Error != nil {
		fail(c, http.StatusNotFound, "not_found", "롤아웃 없음", nil)
		return ro, a, false
	}
	s.db(c).Unscoped().First(&a, "id = ?", ro.AgentID)
	return ro, a, true
}

func (s *Server) getRollout(c *gin.Context) {
	ro, a, ok := s.findRollout(c)
	if ok && s.require(c, "viewer", projObj(ro.OrgID, a.Project)) {
		c.JSON(http.StatusOK, s.rolloutView(s.db(c), ro))
	}
}

func (s *Server) listRollouts(c *gin.Context) {
	a, ok := s.findAgent(c, "viewer")
	if !ok {
		return
	}
	var items []store.Rollout
	s.db(c).Where("agent_id = ?", a.ID).Order("created_at DESC").Limit(50).Find(&items)
	out := make([]rolloutView, 0, len(items))
	for _, r := range items {
		out = append(out, s.rolloutView(s.db(c), r))
	}
	c.JSON(http.StatusOK, gin.H{"items": out})
}

// advanceRollout: 다음 가중치 단계 (마지막이면 승격). 첫 전진 때 평가 게이트(suite)를 돌린다.
func (s *Server) advanceRollout(c *gin.Context) {
	ro, a, ok := s.findRollout(c)
	if !ok || !s.require(c, "deploy", envObj(ro.OrgID, a.Project, ro.Env)) {
		return
	}
	if ro.Status != "running" {
		fail(c, http.StatusConflict, "rollout_"+ro.Status, "진행 중이 아님", nil)
		return
	}
	db := s.db(c)
	var d store.Deployment
	db.First(&d, "id = ?", ro.ToDeploymentID)
	if d.Status != store.DeployReady {
		fail(c, http.StatusConflict, "canary_not_ready", "canary 가 ready 가 아님 ("+d.Status+")", nil)
		return
	}
	var steps []int
	_ = json.Unmarshal([]byte(ro.Steps), &steps)
	var g rolloutGate
	_ = json.Unmarshal([]byte(ro.Gate), &g)
	if reason := s.gateFailure(c, g, d); reason != "" {
		s.rollback(c, &ro, reason)
		c.JSON(http.StatusConflict, gin.H{"error": "gate_failed", "message": reason, "rollout": s.rolloutView(db, ro)})
		return
	}
	if ro.StepIndex == 0 && g.Suite != nil {
		if res, err := s.runSuite(c, g.Suite, d); err != nil || res["gate"] != "pass" {
			reason := fmt.Sprintf("평가 게이트 실패: %v %v", res["gate"], err)
			s.rollback(c, &ro, reason)
			c.JSON(http.StatusConflict, gin.H{"error": "gate_failed", "message": reason, "eval": res, "rollout": s.rolloutView(db, ro)})
			return
		} else {
			appendHistory(&ro, map[string]any{"event": "eval_passed", "run": res["run_id"]})
		}
	}
	next := ro.StepIndex + 1
	if next >= len(steps) || steps[next] >= 100 {
		s.promote(c, &ro, d)
		c.JSON(http.StatusOK, s.rolloutView(db, ro))
		return
	}
	ro.StepIndex = next
	appendHistory(&ro, map[string]any{"event": "advanced", "weight": steps[next]})
	ro.UpdatedAt = time.Now().UTC()
	db.Model(&store.Deployment{}).Where("id = ?", d.ID).Updates(map[string]any{"weight": steps[next], "updated_at": ro.UpdatedAt})
	db.Save(&ro)
	s.emit(c, "plt.deploy.rollout.advanced", gin.H{"org": ro.OrgID, "id": ro.ID, "agentId": ro.AgentID, "env": ro.Env, "weight": steps[next]})
	c.JSON(http.StatusOK, s.rolloutView(db, ro))
}

// gateFailure: canary 실패 상태 또는 오류율 초과면 사유.
func (s *Server) gateFailure(_ *gin.Context, g rolloutGate, d store.Deployment) string {
	if d.Status == store.DeployFailed {
		return "canary 실패: " + deref(d.Message)
	}
	if d.Calls >= g.MinCalls && d.Calls > 0 && float64(d.Errors)/float64(d.Calls) > g.MaxErrorRate {
		return fmt.Sprintf("canary 오류율 %.2f > %.2f (%d/%d)", float64(d.Errors)/float64(d.Calls), g.MaxErrorRate, d.Errors, d.Calls)
	}
	return ""
}

func deref(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

// runSuite 는 EV 회귀 스위트를 canary 대상으로 돌린다 ({{canary.endpoint}} 치환).
func (s *Server) runSuite(c *gin.Context, suite map[string]any, d store.Deployment) (map[string]any, error) {
	ev, ok := s.Services["ev"]
	if !ok {
		return nil, errors.New("ev 서비스 미설정 (ATLAS_SVC_URLS)")
	}
	b, _ := json.Marshal(suite)
	b = bytes.ReplaceAll(b, []byte("{{canary.endpoint}}"), []byte(d.Endpoint))
	req, _ := http.NewRequestWithContext(c, http.MethodPost, strings.TrimRight(ev.String(), "/")+"/v1/runs", bytes.NewReader(b))
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 5 * time.Minute}
	res, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	out := map[string]any{}
	raw, _ := io.ReadAll(io.LimitReader(res.Body, 1<<20))
	_ = json.Unmarshal(raw, &out)
	if res.StatusCode >= 300 {
		return out, fmt.Errorf("ev %d", res.StatusCode)
	}
	return out, nil
}

// promote: canary → primary(100), 이전 primary → standby(즉시 롤백용, 이전 standby 는 중지).
func (s *Server) promote(c *gin.Context, ro *store.Rollout, d store.Deployment) {
	db := s.db(c)
	now := time.Now().UTC()
	_ = db.Transaction(func(tx *gorm.DB) error {
		tx.Model(&store.Deployment{}).Where("agent_id = ? AND env = ? AND slot = ? AND status <> ?", ro.AgentID, ro.Env, store.SlotStandby, store.DeployStopped).
			Updates(map[string]any{"status": store.DeployStopped, "updated_at": now})
		tx.Model(&store.Deployment{}).Where("agent_id = ? AND env = ? AND slot = ? AND status <> ? AND id <> ?", ro.AgentID, ro.Env, store.SlotPrimary, store.DeployStopped, d.ID).
			Updates(map[string]any{"slot": store.SlotStandby, "weight": 0, "updated_at": now})
		return tx.Model(&store.Deployment{}).Where("id = ?", d.ID).Updates(map[string]any{"slot": store.SlotPrimary, "weight": 100, "updated_at": now}).Error
	})
	ro.Status, ro.UpdatedAt = "promoted", now
	appendHistory(ro, map[string]any{"event": "promoted"})
	db.Save(ro)
	s.audit(c, ro.OrgID, "deploy.rollout.promote", "rollout:"+ro.ID, "success", nil, gin.H{"deployment": d.ID, "rev": ro.Rev}, nil)
	s.emit(c, "plt.deploy.rollout.promoted", gin.H{"org": ro.OrgID, "id": ro.ID, "agentId": ro.AgentID, "env": ro.Env, "rev": ro.Rev})
}

// rollback: canary 중지, primary 가중치 복원. 요청 트랜잭션 밖(s.DB)에 쓴다 — 게이트 실패는 4xx 로 응답하므로
// 요청 트랜잭션이 롤백돼도 롤백 기록은 남아야 한다.
func (s *Server) rollback(ctx context.Context, ro *store.Rollout, reason string) {
	db := s.DB.WithContext(ctx)
	now := time.Now().UTC()
	db.Model(&store.Deployment{}).Where("id = ?", ro.ToDeploymentID).Updates(map[string]any{"status": store.DeployStopped, "weight": 0, "updated_at": now})
	ro.Status, ro.Reason, ro.UpdatedAt = "rolled_back", &reason, now
	appendHistory(ro, map[string]any{"event": "rolled_back", "reason": reason})
	db.Save(ro)
	if gc, ok := ctx.(*gin.Context); ok {
		s.audit(gc, ro.OrgID, "deploy.rollout.rollback", "rollout:"+ro.ID, "success", nil, gin.H{"reason": reason}, nil)
	}
	s.emit(ctx, "plt.deploy.rollout.rolled_back", gin.H{"org": ro.OrgID, "id": ro.ID, "agentId": ro.AgentID, "env": ro.Env, "reason": reason})
}

func (s *Server) abortRollout(c *gin.Context) {
	ro, a, ok := s.findRollout(c)
	if !ok || !s.require(c, "deploy", envObj(ro.OrgID, a.Project, ro.Env)) {
		return
	}
	if ro.Status != "running" {
		fail(c, http.StatusConflict, "rollout_"+ro.Status, "진행 중이 아님", nil)
		return
	}
	var b struct {
		Reason string `json:"reason"`
	}
	_ = c.ShouldBindJSON(&b)
	s.rollback(c, &ro, firstNonEmpty(b.Reason, "수동 중단"))
	c.JSON(http.StatusOK, s.rolloutView(s.db(c), ro))
}

// EvaluateRollouts 는 진행 중 롤아웃의 canary 를 주기 점검해 실패·오류율 초과면 자동 롤백한다 (prober 주기).
func (s *Server) EvaluateRollouts(ctx context.Context) {
	var items []store.Rollout
	if s.DB.WithContext(ctx).Where("status = ?", "running").Find(&items).Error != nil {
		return
	}
	for i := range items {
		ro := items[i]
		var g rolloutGate
		_ = json.Unmarshal([]byte(ro.Gate), &g)
		if g.AutoRollback != nil && !*g.AutoRollback {
			continue
		}
		var d store.Deployment
		if s.DB.WithContext(ctx).First(&d, "id = ?", ro.ToDeploymentID).Error != nil {
			continue
		}
		if reason := s.gateFailure(nil, g, d); reason != "" {
			slog.Info("rollout auto rollback", "id", ro.ID, "reason", reason)
			s.rollback(ctx, &ro, "자동 롤백: "+reason)
		}
	}
}

// ---- 롤백 (DEP-07) ----

func (s *Server) rollbackDeployment(c *gin.Context) {
	d, a, ok := s.findDeployment(c, "deploy")
	if !ok {
		return
	}
	if d.Slot != store.SlotPrimary {
		fail(c, http.StatusConflict, "not_primary", "primary 배포만 롤백", nil)
		return
	}
	var b struct {
		ToRev int `json:"toRev"`
	}
	_ = c.ShouldBindJSON(&b)
	db := s.db(c)
	now := time.Now().UTC()
	var sb store.Deployment
	if b.ToRev == 0 && db.Where("agent_id = ? AND env = ? AND slot = ? AND status = ?", d.AgentID, d.Env, store.SlotStandby, store.DeployReady).First(&sb).Error == nil {
		_ = db.Transaction(func(tx *gorm.DB) error { // 맞바꿈 — 유니크 인덱스(agent, env, slot) 때문에 임시 슬롯을 거친다
			tx.Model(&store.Deployment{}).Where("id = ?", d.ID).Update("slot", "swap")
			tx.Model(&store.Deployment{}).Where("id = ?", sb.ID).Updates(map[string]any{"slot": store.SlotPrimary, "weight": 100, "updated_at": now})
			return tx.Model(&store.Deployment{}).Where("id = ?", d.ID).Updates(map[string]any{"slot": store.SlotStandby, "weight": 0, "updated_at": now}).Error
		})
		s.audit(c, d.OrgID, "deploy.rollback", "deployment:"+d.ID, "success", gin.H{"rev": d.Rev, "deployment": d.ID}, gin.H{"rev": sb.Rev, "deployment": sb.ID}, map[string]any{"mode": "standby_swap"})
		s.emit(c, "plt.deploy.rolled_back", gin.H{"org": d.OrgID, "agentId": d.AgentID, "env": d.Env, "from": d.ID, "to": sb.ID, "rev": sb.Rev, "mode": "standby_swap"})
		db.First(&sb, "id = ?", sb.ID)
		c.JSON(http.StatusOK, gin.H{"mode": "standby_swap", "primary": s.deploymentView(sb, a), "standby": d.ID})
		return
	}
	to := firstNonZero(b.ToRev, d.Rev-1)
	var rv store.Revision
	if to < 1 || db.First(&rv, "agent_id = ? AND rev = ?", d.AgentID, to).Error != nil {
		fail(c, http.StatusConflict, "no_previous", "되돌릴 standby·이전 리비전 없음", nil)
		return
	}
	db.Model(&store.Deployment{}).Where("id = ?", d.ID).Updates(map[string]any{"rev": to, "status": store.DeployStarting, "fail_count": 0, "updated_at": now})
	s.audit(c, d.OrgID, "deploy.rollback", "deployment:"+d.ID, "success", gin.H{"rev": d.Rev}, gin.H{"rev": to}, map[string]any{"mode": "revision"})
	s.emit(c, "plt.deploy.rolled_back", gin.H{"org": d.OrgID, "agentId": d.AgentID, "env": d.Env, "from": d.Rev, "to": to, "mode": "revision"})
	db.First(&d, "id = ?", d.ID)
	c.JSON(http.StatusOK, gin.H{"mode": "revision", "primary": s.deploymentView(d, a)})
}

// ---- 스케일 (MGR-07) ----

func (s *Server) scaleDeployment(c *gin.Context) {
	d, a, ok := s.findDeployment(c, "deploy")
	if !ok {
		return
	}
	var b struct {
		Replicas int `json:"replicas"`
	}
	if err := c.ShouldBindJSON(&b); err != nil || b.Replicas < 0 || b.Replicas > 50 {
		fail(c, http.StatusBadRequest, "bad_request", "replicas 0~50", nil)
		return
	}
	s.db(c).Model(&store.Deployment{}).Where("id = ?", d.ID).Updates(map[string]any{"replicas": b.Replicas, "updated_at": time.Now().UTC()})
	s.audit(c, d.OrgID, "deploy.scale", "deployment:"+d.ID, "success", gin.H{"replicas": d.Replicas}, gin.H{"replicas": b.Replicas}, nil)
	s.emit(c, "plt.deploy.scaled", gin.H{"org": d.OrgID, "id": d.ID, "agentId": d.AgentID, "env": d.Env, "replicas": b.Replicas, "driver": d.Driver})
	d.Replicas = b.Replicas
	applied := d.Driver == "kubernetes"
	c.JSON(http.StatusOK, gin.H{"deployment": s.deploymentView(d, a), "desired": b.Replicas, "applied": applied,
		"note": map[bool]string{true: "kubernetes 매니페스트(GET …/manifests) 에 반영", false: "compose·local 드라이버는 선언만 기록 (docker compose up --scale 로 적용)"}[applied]})
}

// ---- 환경 승격 (DEP-08) ----

var envOrder = map[string]int{"dev": 0, "staging": 1, "prod": 2}

type promoteRequest struct {
	From     string `json:"from"`
	To       string `json:"to"`
	Endpoint string `json:"endpoint"`
	Driver   string `json:"driver"`
	Reason   string `json:"reason"`
}

func (s *Server) promoteAgent(c *gin.Context) {
	a, ok := s.findAgent(c, "viewer")
	if !ok {
		return
	}
	var r promoteRequest
	if err := c.ShouldBindJSON(&r); err != nil {
		fail(c, http.StatusBadRequest, "bad_request", err.Error(), nil)
		return
	}
	fo, okF := envOrder[r.From]
	to, okT := envOrder[r.To]
	if !okF || !okT || to != fo+1 {
		fail(c, http.StatusUnprocessableEntity, "invalid_promotion", "승격은 dev→staging→prod 한 단계씩", gin.H{"from": r.From, "to": r.To})
		return
	}
	if _, ok := s.orgGuard(c, a.OrgID); !ok || !s.require(c, "deploy", envObj(a.OrgID, a.Project, r.To)) {
		return
	}
	db := s.db(c)
	var src store.Deployment
	if db.Where("agent_id = ? AND env = ? AND slot = ? AND status = ?", a.ID, r.From, store.SlotPrimary, store.DeployReady).First(&src).Error != nil {
		fail(c, http.StatusConflict, "source_not_ready", r.From+" 에 ready primary 배포 없음", nil)
		return
	}
	var rev store.Revision
	db.First(&rev, "agent_id = ? AND rev = ?", a.ID, src.Rev)
	if !s.enforcePolicy(c, a.OrgID, r.To, rev, a) {
		return
	}
	r.Endpoint, r.Driver = firstNonEmpty(r.Endpoint, src.Endpoint), firstNonEmpty(r.Driver, src.Driver)
	if !validEndpoint(r.Driver, r.Endpoint) {
		fail(c, http.StatusUnprocessableEntity, "invalid_deployment", "endpoint·driver 확인", nil)
		return
	}
	p, _ := s.policyFor(db, a.OrgID, r.To)
	payload := map[string]any{"agentId": a.ID, "from": r.From, "to": r.To, "rev": src.Rev, "endpoint": r.Endpoint, "driver": r.Driver, "reason": r.Reason}
	if p.RequireApproval {
		body, _ := json.Marshal(payload)
		now := time.Now().UTC()
		ap := store.Approval{ID: iam.ULID(), OrgID: a.OrgID, Project: a.Project, Source: "deploy", SourceRef: "promote:" + a.ID + ":" + r.To, AgentID: a.ID, UnitID: a.UnitID,
			Action: "deploy.promote", RiskLevel: map[bool]string{true: "high", false: "medium"}[r.To == "prod"], Payload: string(body), PayloadHash: payloadHash(body),
			Requester: firstNonEmpty(principal(c).Sub, "anonymous"), Status: "pending", ExpiresAt: now.Add(72 * time.Hour), CreatedAt: now}
		if err := db.Create(&ap).Error; err != nil {
			fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
			return
		}
		s.audit(c, a.OrgID, "approval.request", "approval:"+ap.ID, "success", nil, gin.H{"payload_hash": ap.PayloadHash}, map[string]any{"action": ap.Action, "to": r.To})
		s.emit(c, "plt.approval.requested", gin.H{"org": a.OrgID, "id": ap.ID, "action": ap.Action, "risk": ap.RiskLevel, "agent": a.Name, "expiresAt": ap.ExpiresAt})
		c.JSON(http.StatusAccepted, gin.H{"status": "pending_approval", "approvalId": ap.ID, "promotion": payload})
		return
	}
	d, err := s.applyPromotion(c, payload)
	if err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "promoted", "deployment": s.deploymentView(d, a)})
}

// applyPromotion 은 대상 환경 primary 를 원본 리비전으로 만들거나 갱신한다 (승인 결정 콜백에서도 호출).
func (s *Server) applyPromotion(ctx context.Context, p map[string]any) (store.Deployment, error) {
	db := s.DB.WithContext(ctx)
	if gc, ok := ctx.(*gin.Context); ok {
		db = s.db(gc)
	}
	agentID, _ := p["agentId"].(string)
	to, _ := p["to"].(string)
	endpoint, _ := p["endpoint"].(string)
	driver, _ := p["driver"].(string)
	rev := int(toFloat(p["rev"]))
	var a store.Agent
	if err := db.First(&a, "id = ?", agentID).Error; err != nil {
		return store.Deployment{}, err
	}
	now := time.Now().UTC()
	var d store.Deployment
	if db.Where("agent_id = ? AND env = ? AND slot = ? AND status <> ?", agentID, to, store.SlotPrimary, store.DeployStopped).First(&d).Error == nil {
		d.Rev, d.Endpoint, d.Driver, d.Status, d.FailCount, d.UpdatedAt = rev, strings.TrimRight(endpoint, "/"), driver, store.DeployStarting, 0, now
		if err := db.Save(&d).Error; err != nil {
			return d, err
		}
	} else {
		d = store.Deployment{ID: uuid.NewString(), OrgID: a.OrgID, AgentID: agentID, Rev: rev, Env: to, Driver: driver, Endpoint: strings.TrimRight(endpoint, "/"),
			Status: store.DeployPending, Slot: store.SlotPrimary, Weight: 100, Replicas: 1, CreatedAt: now, UpdatedAt: now}
		if err := db.Create(&d).Error; err != nil {
			return d, err
		}
	}
	s.emit(ctx, "plt.deploy.promoted", gin.H{"org": a.OrgID, "agentId": agentID, "from": p["from"], "to": to, "rev": rev, "deployment": d.ID})
	if gc, ok := ctx.(*gin.Context); ok {
		s.audit(gc, a.OrgID, "deploy.promote", "deployment:"+d.ID, "success", nil, gin.H{"rev": rev, "env": to}, map[string]any{"from": p["from"]})
	}
	return d, nil
}

func toFloat(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case int:
		return float64(x)
	}
	return 0
}

// ---- 토폴로지 (MGR-09) ----

func (s *Server) topology(c *gin.Context) {
	org := orgOf(c)
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	env := c.DefaultQuery("env", "dev")
	s.tenantScope(c, org)
	db := s.db(c)
	var deps []store.Deployment
	db.Where("org_id = ? AND env = ? AND status <> ?", org, env, store.DeployStopped).Find(&deps)
	type node struct {
		ID          string           `json:"id"`
		Name        string           `json:"name"`
		UnitID      string           `json:"unitId"`
		Deployments []map[string]any `json:"deployments"`
	}
	nodes := map[string]*node{}
	for _, d := range deps {
		n, ok := nodes[d.AgentID]
		if !ok {
			var a store.Agent
			db.Unscoped().First(&a, "id = ?", d.AgentID)
			n = &node{ID: a.ID, Name: a.Name, UnitID: a.UnitID}
			nodes[d.AgentID] = n
		}
		n.Deployments = append(n.Deployments, map[string]any{"id": d.ID, "slot": d.Slot, "weight": d.Weight, "status": d.Status, "rev": d.Rev, "replicas": d.Replicas})
	}
	var edges []store.TopologyEdge
	db.Where("org_id = ? AND env = ?", org, env).Order("calls DESC").Find(&edges)
	out := make([]*node, 0, len(nodes))
	for _, n := range nodes {
		out = append(out, n)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	c.JSON(http.StatusOK, gin.H{"org": org, "env": env, "nodes": out, "edges": edges})
}

// ---- Kubernetes 매니페스트 (DEP-03 — 렌더·드라이런, 적용은 GitOps/kubectl) ----

func (s *Server) deploymentManifests(c *gin.Context) {
	d, a, ok := s.findDeployment(c, "viewer")
	if !ok {
		return
	}
	var rev store.Revision
	s.db(c).First(&rev, "agent_id = ? AND rev = ?", d.AgentID, d.Rev)
	m, err := manifest.Parse([]byte(rev.Manifest))
	if err != nil {
		fail(c, http.StatusUnprocessableEntity, "invalid_manifest", err.Error(), nil)
		return
	}
	docs := RenderK8s(m, a, d)
	var buf bytes.Buffer
	for i, doc := range docs {
		if i > 0 {
			buf.WriteString("---\n")
		}
		y, _ := yaml.Marshal(doc)
		buf.Write(y)
	}
	c.Data(http.StatusOK, "application/yaml; charset=utf-8", buf.Bytes())
}

// RenderK8s 는 Deployment + Service (+ canary 는 같은 Service 선택자, 가중치는 주석) 를 만든다. 보안 기본값: 비루트·읽기 전용 rootfs·권한 상승 금지.
func RenderK8s(m *manifest.Manifest, a store.Agent, d store.Deployment) []map[string]any {
	name := a.Name
	if d.Slot != store.SlotPrimary {
		name += "-" + d.Slot
	}
	port := 8000
	if len(m.Spec.Interfaces) > 0 && m.Spec.Interfaces[0].Port > 0 {
		port = m.Spec.Interfaces[0].Port
	}
	image := m.Spec.Artifacts.Image
	if image == "" {
		image = "ghcr.io/prodai/atlas-agent-runtime:" + m.Spec.Version
	}
	labels := map[string]any{"app.kubernetes.io/name": a.Name, "app.kubernetes.io/part-of": "prodai-atlas", "atlas.prodai.io/unit-id": a.UnitID, "atlas.prodai.io/slot": d.Slot}
	health, _ := func() (map[string]any, bool) {
		sp, _ := m.Raw["spec"].(map[string]any)
		h, ok := sp["health"].(map[string]any)
		return h, ok
	}()
	hp := func(k, def string) string {
		if v, ok := health[k].(string); ok && v != "" {
			return v
		}
		return def
	}
	probe := func(path string) map[string]any {
		return map[string]any{"httpGet": map[string]any{"path": path, "port": port}, "periodSeconds": 10}
	}
	dep := map[string]any{"apiVersion": "apps/v1", "kind": "Deployment",
		"metadata": map[string]any{"name": name, "namespace": "atlas-" + d.Env, "labels": labels,
			"annotations": map[string]any{"atlas.prodai.io/rev": fmt.Sprint(d.Rev), "atlas.prodai.io/weight": fmt.Sprint(d.Weight), "atlas.prodai.io/deployment-id": d.ID}},
		"spec": map[string]any{"replicas": d.Replicas, "selector": map[string]any{"matchLabels": map[string]any{"app.kubernetes.io/name": a.Name, "atlas.prodai.io/slot": d.Slot}},
			"template": map[string]any{"metadata": map[string]any{"labels": labels},
				"spec": map[string]any{"automountServiceAccountToken": false, "securityContext": map[string]any{"runAsNonRoot": true, "runAsUser": 65532, "seccompProfile": map[string]any{"type": "RuntimeDefault"}},
					"containers": []any{map[string]any{"name": "agent", "image": image, "ports": []any{map[string]any{"containerPort": port}},
						"env":            []any{map[string]any{"name": "ATLAS_DEPLOY_ENV", "value": d.Env}, map[string]any{"name": "ATLAS_PUBLIC_URL", "value": fmt.Sprintf("http://%s.atlas-%s.svc:%d", a.Name, d.Env, port)}},
						"readinessProbe": probe(hp("readiness", "/readyz")), "livenessProbe": probe(hp("liveness", "/healthz")),
						"securityContext": map[string]any{"readOnlyRootFilesystem": true, "allowPrivilegeEscalation": false, "capabilities": map[string]any{"drop": []any{"ALL"}}},
						"resources":       map[string]any{"requests": map[string]any{"cpu": "100m", "memory": "256Mi"}, "limits": map[string]any{"memory": "1Gi"}}}}}}}}
	svc := map[string]any{"apiVersion": "v1", "kind": "Service", "metadata": map[string]any{"name": name, "namespace": "atlas-" + d.Env, "labels": labels},
		"spec": map[string]any{"selector": map[string]any{"app.kubernetes.io/name": a.Name, "atlas.prodai.io/slot": d.Slot}, "ports": []any{map[string]any{"port": port, "targetPort": port}}}}
	return []map[string]any{dep, svc}
}
