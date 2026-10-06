// Package api 는 Manager REST API(/api/v1, PRD §17.2)다.
package api

import (
	"context"
	_ "embed"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/gorm"

	"atlas-manager/internal/catalog"
	"atlas-manager/internal/events"
	"atlas-manager/internal/iam"
	"atlas-manager/internal/manifest"
	"atlas-manager/internal/notify"
	"atlas-manager/internal/secrets"
	"atlas-manager/internal/store"
)

//go:embed openapi.json
var openapiSpec []byte

// Server 는 API 핸들러의 의존성이다.
type Server struct {
	DB       *gorm.DB
	Events   events.Publisher
	Env      string
	Engine   string
	Proxy    *http.Client        // agent-proxy 용
	Services map[string]*url.URL // /svc/{name} 프록시 대상 (콘솔용)
	UI       gin.HandlerFunc     // nil 이 아니면 콘솔 SPA 를 서빙
	Version  string
	// PLT-IAM · PLT-TEN (M2)
	AuthMode   string         // off | oidc
	OIDC       *iam.OIDC      // AuthMode=oidc 일 때 토큰 검증
	OIDCPublic gin.H          // 콘솔 로그인 설정 {issuer, clientId}
	FGA        *iam.FGA       // nil 이면 사용자 인가 불가(플랫폼 관리자·API 키만)
	Audit      *iam.Audit     // 해시 체인 감사 로그
	Signer     *iam.Signer    // A2A 테넌트 JWT
	KeyEnv     string         // API 키 접두사 env (atl_{env}_…)
	Secrets    secrets.Store  // 비밀 백엔드 (PLT-SEC) — nil 이면 비밀 API 503
	Notify     *notify.Engine // 알림 전달 (X-NTF) — 규칙 URL 검사·시험 발송
}

const defaultProject = "default"

// Router 는 gin 라우터를 만든다.
func (s *Server) Router() *gin.Engine {
	r := gin.New()
	r.Use(gin.Recovery())
	r.GET("/health", s.health)
	r.GET("/.well-known/jwks.json", s.jwks)
	v1 := r.Group("/api/v1", s.authenticate, s.txMiddleware)
	v1.GET("/openapi.json", func(c *gin.Context) { c.Data(http.StatusOK, "application/json", openapiSpec) })
	v1.GET("/auth/config", s.authConfig)
	v1.GET("/auth/me", s.me)
	v1.GET("/orgs", s.listOrgs)
	v1.POST("/orgs", s.createOrg)
	v1.GET("/orgs/:org", s.getOrg)
	v1.PATCH("/orgs/:org", s.patchOrg)
	v1.DELETE("/orgs/:org", s.deleteOrg)
	v1.GET("/orgs/:org/export", s.exportOrg)
	v1.GET("/orgs/:org/projects", s.listProjects)
	v1.POST("/orgs/:org/projects", s.createProject)
	v1.GET("/orgs/:org/projects/:proj/envs", s.listEnvs)
	v1.POST("/orgs/:org/projects/:proj/envs", s.createEnv)
	v1.GET("/orgs/:org/quotas", s.listQuotas)
	v1.PUT("/orgs/:org/quotas", s.putQuota)
	v1.GET("/orgs/:org/members", s.listMembers)
	v1.POST("/orgs/:org/members", s.addMember)
	v1.DELETE("/orgs/:org/members", s.removeMember)
	v1.GET("/orgs/:org/apikeys", s.listAPIKeys)
	v1.POST("/orgs/:org/apikeys", s.createAPIKey)
	v1.DELETE("/orgs/:org/apikeys/:id", s.revokeAPIKey)
	v1.GET("/orgs/:org/audit", s.listAudit)
	v1.GET("/orgs/:org/audit/verify", s.verifyAudit)
	v1.GET("/orgs/:org/audit/root", s.auditRoot)
	v1.GET("/orgs/:org/audit/export", s.exportAudit)
	v1.GET("/orgs/:org/secrets", s.listSecrets)
	v1.PUT("/orgs/:org/secrets/:name", s.putSecret)
	v1.POST("/orgs/:org/secrets/:name/rotate", s.rotateSecret)
	v1.DELETE("/orgs/:org/secrets/:name", s.deleteSecret)
	v1.POST("/agents/:id/token", s.agentToken)
	v1.GET("/secrets/:name", s.readSecret)
	v1.GET("/catalog/units", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"items": catalog.All()}) })
	v1.GET("/agents", s.listAgents)
	v1.POST("/agents", s.createAgent)
	v1.GET("/agents/:id", s.getAgent)
	v1.PATCH("/agents/:id", s.updateAgent)
	v1.DELETE("/agents/:id", s.deleteAgent)
	v1.GET("/agents/:id/versions", s.listVersions)
	v1.POST("/agents/:id/a2a", s.proxyA2A)
	v1.GET("/agents/:id/card", s.getCard)
	v1.GET("/a2a/registry/agents", s.registry)
	v1.POST("/events", s.relayEvent)
	v1.POST("/approvals", s.createApproval)
	v1.GET("/approvals", s.listApprovals)
	v1.GET("/approvals/:id", s.getApproval)
	v1.POST("/approvals/:id/decision", s.decideApproval)
	v1.POST("/approvals/withdraw", s.withdrawApprovals)
	v1.POST("/approvals/evaluate", s.evaluateApproval)
	v1.GET("/approvals-stats", s.approvalStats)
	v1.GET("/orgs/:org/approval-policy", s.getApprovalPolicy)
	v1.PUT("/orgs/:org/approval-policy", s.putApprovalPolicy)
	v1.PUT("/orgs/:org/approval-delegations", s.putDelegation)
	v1.GET("/auth/check", s.authCheck)
	v1.POST("/token/exchange", s.tokenExchange)
	v1.GET("/orgs/:org/security-policy", s.getSecurityPolicy)
	v1.PUT("/orgs/:org/security-policy", s.putSecurityPolicy)
	v1.GET("/orgs/:org/scim/group-roles", s.listGroupRoles)
	v1.PUT("/orgs/:org/scim/group-roles", s.changeGroupRole)
	v1.DELETE("/orgs/:org/scim/group-roles", s.changeGroupRole)
	v1.GET("/orgs/:org/budgets", s.listBudgetsM)
	v1.PUT("/orgs/:org/budgets/:id", s.putBudgetM)
	v1.DELETE("/orgs/:org/budgets/:id", s.deleteBudgetM)
	v1.GET("/orgs/:org/usage", s.orgUsage)
	v1.GET("/plans", s.listPlans)
	v1.PUT("/plans/:id", s.putPlan)
	v1.GET("/orgs/:org/notifications/rules", s.listRules)
	v1.POST("/orgs/:org/notifications/rules", s.createRule)
	v1.PUT("/orgs/:org/notifications/rules/:id", s.updateRule)
	v1.DELETE("/orgs/:org/notifications/rules/:id", s.deleteRule)
	v1.POST("/orgs/:org/notifications/rules/:id/test", s.testRule)
	v1.GET("/orgs/:org/notifications/deliveries", s.listDeliveries)
	v1.POST("/orgs/:org/notifications/:id/ack", s.ackNotification)
	v1.GET("/notifications/templates", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"items": notify.Templates}) })
	v1.GET("/deployments", s.listDeployments)
	v1.POST("/deployments", s.createDeployment)
	v1.GET("/deployments/:id", s.getDeployment)
	v1.DELETE("/deployments/:id", s.stopDeployment)
	v1.POST("/deployments/:id/rollback", s.rollbackDeployment)
	v1.PUT("/deployments/:id/scale", s.scaleDeployment)
	v1.GET("/deployments/:id/manifests", s.deploymentManifests)
	v1.POST("/agents/:id/rollouts", s.createRollout)
	v1.GET("/agents/:id/rollouts", s.listRollouts)
	v1.GET("/rollouts/:rid", s.getRollout)
	v1.POST("/rollouts/:rid/advance", s.advanceRollout)
	v1.POST("/rollouts/:rid/abort", s.abortRollout)
	v1.POST("/agents/:id/promote", s.promoteAgent)
	v1.GET("/orgs/:org/deploy-policies/:env", s.getDeployPolicy)
	v1.PUT("/orgs/:org/deploy-policies/:env", s.putDeployPolicy)
	v1.GET("/topology", s.topology)
	v1.POST("/manifests/digest", s.manifestDigest)
	for _, m := range []string{http.MethodGet, http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete} {
		r.Handle(m, "/svc/:name/*path", s.authenticate, s.proxyService)
	}
	sc := r.Group("/scim/v2", s.authenticate, s.txMiddleware, s.scimAuth) // SCIM 2.0 (FR-PLT-IAM-03)
	sc.GET("/ServiceProviderConfig", s.scimConfig)
	sc.GET("/Users", s.scimListUsers)
	sc.POST("/Users", s.scimCreateUser)
	sc.GET("/Users/:id", s.scimGetUser)
	sc.PUT("/Users/:id", s.scimReplaceUser)
	sc.PATCH("/Users/:id", s.scimPatchUser)
	sc.DELETE("/Users/:id", s.scimDeleteUser)
	sc.GET("/Groups", s.scimListGroups)
	sc.POST("/Groups", s.scimCreateGroup)
	sc.GET("/Groups/:id", s.scimGetGroup)
	sc.PUT("/Groups/:id", s.scimReplaceGroup)
	sc.PATCH("/Groups/:id", s.scimPatchGroup)
	sc.DELETE("/Groups/:id", s.scimDeleteGroup)
	if s.UI != nil {
		r.NoRoute(s.UI)
	}
	return r
}

func (s *Server) health(c *gin.Context) {
	db := gin.H{"engine": s.Engine, "status": "ok"}
	code := http.StatusOK
	if sqlDB, err := s.DB.DB(); err != nil || sqlDB.PingContext(c) != nil {
		db["status"], code = "error", http.StatusServiceUnavailable
	}
	c.JSON(code, gin.H{"status": map[bool]string{true: "ok", false: "degraded"}[code == http.StatusOK], "env": s.Env, "db": db,
		"version": s.Version, "component": "atlas-manager"})
}

// agentView 는 응답 표현이다(현재 리비전 요약 포함).
type agentView struct {
	store.Agent
	Version  string         `json:"version"`
	SpecHash string         `json:"specHash"`
	Manifest map[string]any `json:"manifest,omitempty"`
}

func fail(c *gin.Context, code int, errCode, msg string, details any) {
	body := gin.H{"error": errCode, "message": msg}
	if details != nil {
		body["details"] = details
	}
	c.AbortWithStatusJSON(code, body)
}

func (s *Server) emit(ctx context.Context, subject string, data any) {
	// 이벤트 실패가 API 를 막지 않는다(카탈로그가 정본, 이벤트는 동기화 보조).
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	if err := s.Events.Publish(ctx, subject, data); err != nil {
		slog.Warn("event publish failed", "subject", subject, "err", err)
	}
}

func readManifest(c *gin.Context) (*manifest.Manifest, bool) {
	body, err := io.ReadAll(io.LimitReader(c.Request.Body, 1<<20))
	if err != nil {
		fail(c, http.StatusBadRequest, "bad_request", err.Error(), nil)
		return nil, false
	}
	m, err := manifest.Parse(body)
	var fe manifest.Errors
	if errors.As(err, &fe) {
		fail(c, http.StatusUnprocessableEntity, "invalid_manifest", "매니페스트 검증 실패", fe)
		return nil, false
	}
	return m, true
}

func (s *Server) view(db *gorm.DB, a store.Agent, withManifest bool) (agentView, error) {
	var rev store.Revision
	if err := db.First(&rev, "agent_id = ? AND rev = ?", a.ID, a.CurrentRev).Error; err != nil {
		return agentView{}, err
	}
	v := agentView{Agent: a, Version: rev.Version, SpecHash: rev.SpecHash}
	if withManifest {
		m, _ := manifest.Parse([]byte(rev.Manifest))
		if m != nil {
			v.Manifest = m.Raw
		}
	}
	return v, nil
}

// findAgent 는 요청 조직 안에서만 찾는다(앱 수준 필터 + PostgreSQL RLS 이중 방어, CMP-01).
// relation 이 있으면 에이전트 프로젝트에 대해 인가한다.
func (s *Server) findAgent(c *gin.Context, relation string) (store.Agent, bool) {
	var a store.Agent
	org := orgOf(c)
	s.tenantScope(c, org)
	err := s.db(c).First(&a, "id = ? AND org_id = ?", c.Param("id"), org).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		fail(c, http.StatusNotFound, "not_found", "에이전트 없음", nil)
		return a, false
	}
	if err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return a, false
	}
	if relation != "" && !s.require(c, relation, projObj(org, a.Project)) {
		return a, false
	}
	return a, true
}

func (s *Server) listAgents(c *gin.Context) {
	org := orgOf(c)
	obj := orgObj(org)
	if p := c.Query("project"); p != "" {
		obj = projObj(org, p)
	}
	if !s.require(c, "viewer", obj) {
		return
	}
	s.tenantScope(c, org)
	db := s.db(c)
	q := db.Where("org_id = ?", org).Order("project, name")
	if p := c.Query("project"); p != "" {
		q = q.Where("project = ?", p)
	}
	if u := c.Query("unit"); u != "" {
		q = q.Where("unit_id = ?", u)
	}
	var agents []store.Agent
	if err := q.Find(&agents).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	items := make([]agentView, 0, len(agents))
	for _, a := range agents {
		v, err := s.view(db, a, false)
		if err != nil {
			fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
			return
		}
		items = append(items, v)
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func (s *Server) createAgent(c *gin.Context) {
	m, ok := readManifest(c)
	if !ok {
		return
	}
	project := c.DefaultQuery("project", defaultProject)
	org := orgOf(c)
	if _, ok := s.orgGuard(c, org); !ok || !s.require(c, "developer", projObj(org, project)) {
		return
	}
	s.tenantScope(c, org)
	db := s.db(c)
	var pn int64
	db.Model(&store.Project{}).Where("org_id = ? AND slug = ?", org, project).Count(&pn)
	if pn == 0 {
		if s.authOn() {
			fail(c, http.StatusNotFound, "project_not_found", "프로젝트 없음: "+project, nil)
			return
		}
		if err := createProjectTx(db, org, project, project, time.Now().UTC()); err != nil { // 개발 모드 편의: 자동 생성
			fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
			return
		}
	}
	if !s.checkQuota(c, db, org, project) {
		return
	}
	now := time.Now().UTC()
	a := store.Agent{ID: uuid.NewString(), OrgID: org, Project: project, Name: m.Metadata.Name, UnitID: m.Metadata.UnitID,
		Layer: m.Metadata.Layer, Concept: m.Metadata.Concept, CurrentRev: 1, CreatedAt: now, UpdatedAt: now}
	rev := store.Revision{AgentID: a.ID, Rev: 1, Version: m.Spec.Version, SpecHash: m.SpecHash(), Manifest: m.JSON(), OrgID: org, CreatedAt: now}
	err := db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Create(&a).Error; err != nil {
			return err
		}
		return tx.Create(&rev).Error
	})
	if errors.Is(err, gorm.ErrDuplicatedKey) {
		fail(c, http.StatusConflict, "already_exists", "같은 프로젝트에 같은 이름의 에이전트가 있음", gin.H{"project": project, "name": a.Name})
		return
	}
	if err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	s.emit(c, "plt.agent.registered", gin.H{"id": a.ID, "org": org, "project": a.Project, "name": a.Name, "unitId": a.UnitID, "rev": 1})
	s.audit(c, org, "agent.create", "agent:"+a.ID, "success", nil, rev.SpecHash, map[string]any{"name": a.Name, "project": a.Project})
	v, _ := s.view(db, a, true)
	c.JSON(http.StatusCreated, v)
}

func (s *Server) getAgent(c *gin.Context) {
	a, ok := s.findAgent(c, "viewer")
	if !ok {
		return
	}
	v, err := s.view(s.db(c), a, true)
	if err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	c.JSON(http.StatusOK, v)
}

// updateAgent 는 전체 매니페스트를 받아 spec 해시가 바뀌면 새 리비전을 만든다(같으면 no-op).
func (s *Server) updateAgent(c *gin.Context) {
	a, ok := s.findAgent(c, "developer")
	if !ok {
		return
	}
	m, ok := readManifest(c)
	if !ok {
		return
	}
	if m.Metadata.Name != a.Name || m.Metadata.UnitID != a.UnitID {
		fail(c, http.StatusUnprocessableEntity, "immutable_field", "metadata.name·unitId 는 바꿀 수 없음", gin.H{"name": a.Name, "unitId": a.UnitID})
		return
	}
	db := s.db(c)
	var cur store.Revision
	if err := db.First(&cur, "agent_id = ? AND rev = ?", a.ID, a.CurrentRev).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	changed := cur.SpecHash != m.SpecHash()
	if changed {
		now := time.Now().UTC()
		rev := store.Revision{AgentID: a.ID, Rev: a.CurrentRev + 1, Version: m.Spec.Version, SpecHash: m.SpecHash(), Manifest: m.JSON(), OrgID: a.OrgID, CreatedAt: now}
		err := db.Transaction(func(tx *gorm.DB) error {
			if err := tx.Create(&rev).Error; err != nil {
				return err
			}
			return tx.Model(&a).Updates(map[string]any{"current_rev": rev.Rev, "updated_at": now}).Error
		})
		if err != nil {
			fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
			return
		}
		a.CurrentRev, a.UpdatedAt = rev.Rev, now
		s.emit(c, "plt.agent.updated", gin.H{"id": a.ID, "project": a.Project, "name": a.Name, "rev": rev.Rev, "specHash": rev.SpecHash})
		s.audit(c, a.OrgID, "agent.update", "agent:"+a.ID, "success", cur.SpecHash, rev.SpecHash, map[string]any{"rev": rev.Rev})
	}
	v, _ := s.view(db, a, true)
	c.JSON(http.StatusOK, gin.H{"changed": changed, "agent": v})
}

// deleteAgent 는 소프트 삭제다(FR-PLT-MGR-04): 배포를 먼저 중지(트래픽 차단)한 뒤 삭제. 보존 기간 후 GC 는 M1.
func (s *Server) deleteAgent(c *gin.Context) {
	a, ok := s.findAgent(c, "developer")
	if !ok {
		return
	}
	var deps []store.Deployment
	if err := s.db(c).Find(&deps, "agent_id = ?", a.ID).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	if err := s.stop(c, deps); err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	if err := s.db(c).Delete(&a).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	s.emit(c, "plt.agent.deleted", gin.H{"id": a.ID, "project": a.Project, "name": a.Name})
	s.audit(c, a.OrgID, "agent.delete", "agent:"+a.ID, "success", a.Name, nil, nil)
	c.Status(http.StatusNoContent)
}

func (s *Server) listVersions(c *gin.Context) {
	a, ok := s.findAgent(c, "viewer")
	if !ok {
		return
	}
	var revs []store.Revision
	if err := s.db(c).Order("rev DESC").Find(&revs, "agent_id = ?", a.ID).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": revs})
}
