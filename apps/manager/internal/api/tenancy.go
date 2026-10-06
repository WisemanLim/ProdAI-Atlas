package api

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"atlas-manager/internal/iam"
	"atlas-manager/internal/store"
)

var slugRe = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{1,62}$`)

var defaultEnvs = []string{"dev", "staging", "prod"}

func (s *Server) jwks(c *gin.Context) {
	if s.Signer == nil {
		c.JSON(http.StatusOK, gin.H{"keys": []any{}})
		return
	}
	c.Header("Cache-Control", "public, max-age=300")
	c.JSON(http.StatusOK, s.Signer.JWKS())
}

func (s *Server) authConfig(c *gin.Context) {
	out := gin.H{"mode": s.AuthMode}
	for k, v := range s.OIDCPublic {
		out[k] = v
	}
	c.JSON(http.StatusOK, out)
}

func (s *Server) me(c *gin.Context) {
	p := principal(c)
	if s.authOn() && p.Kind == "anonymous" {
		fail(c, http.StatusUnauthorized, "unauthenticated", "로그인 필요", nil)
		return
	}
	c.JSON(http.StatusOK, gin.H{"principal": p, "org": orgOf(c)})
}

// ---- 조직 (FR-PLT-TEN-01) ----

type orgRequest struct {
	Slug       string `json:"slug"`
	Name       string `json:"name"`
	Plan       string `json:"plan"`
	DataRegion string `json:"dataRegion"`
	Isolation  string `json:"isolation"`
	Owner      string `json:"owner"` // 소유자 sub (없으면 생성자)
	Status     string `json:"status"`
}

func (s *Server) listOrgs(c *gin.Context) {
	var orgs []store.Organization
	if err := s.DB.WithContext(c).Order("slug").Find(&orgs).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	items := []store.Organization{}
	for _, o := range orgs {
		if s.can(c, "viewer", orgObj(o.Slug)) {
			items = append(items, o)
		}
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func (s *Server) createOrg(c *gin.Context) {
	p := principal(c)
	if !p.PlatformAdmin {
		fail(c, http.StatusForbidden, "forbidden", "조직 생성은 플랫폼 관리자만", nil)
		return
	}
	var r orgRequest
	if err := c.ShouldBindJSON(&r); err != nil || !slugRe.MatchString(r.Slug) {
		fail(c, http.StatusBadRequest, "bad_request", "slug: 소문자·숫자·- 2~63자", nil)
		return
	}
	if r.DataRegion == "" {
		r.DataRegion = "kr"
	}
	if r.Isolation == "" {
		r.Isolation = "shared"
	}
	if r.Plan == "" {
		r.Plan = "self-hosted"
	}
	switch {
	case !map[string]bool{"kr": true, "us": true, "eu": true}[r.DataRegion]:
		fail(c, http.StatusBadRequest, "bad_request", "dataRegion = kr | us | eu", nil)
		return
	case !map[string]bool{"shared": true, "namespace": true, "dedicated": true}[r.Isolation]:
		fail(c, http.StatusBadRequest, "bad_request", "isolation = shared | namespace | dedicated", nil)
		return
	}
	now := time.Now().UTC()
	o := store.Organization{ID: iam.ULID(), Slug: r.Slug, Name: firstNonEmpty(r.Name, r.Slug), Plan: r.Plan, Status: "active",
		DataRegion: r.DataRegion, Isolation: r.Isolation, CreatedAt: now, UpdatedAt: now}
	err := s.DB.WithContext(c).Transaction(func(tx *gorm.DB) error {
		if err := tx.Create(&o).Error; err != nil {
			return err
		}
		return createProjectTx(tx, o.Slug, "default", "Default", now)
	})
	if errors.Is(err, gorm.ErrDuplicatedKey) {
		fail(c, http.StatusConflict, "already_exists", "같은 slug 의 조직이 있음", nil)
		return
	}
	if err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	owner := firstNonEmpty(r.Owner, p.Sub)
	if s.FGA != nil {
		ts := append(iam.Structure(o.Slug, "default", defaultEnvs), iam.Tuple{User: "user:" + owner, Relation: "owner", Object: orgObj(o.Slug)})
		if err := s.FGA.Write(c, ts, nil); err != nil {
			fail(c, http.StatusBadGateway, "authz_backend", err.Error(), nil)
			return
		}
	}
	s.audit(c, o.Slug, "org.create", orgObj(o.Slug), "success", nil, o, map[string]any{"owner": owner})
	s.emit(c, "plt.org.created", gin.H{"org": o.Slug, "plan": o.Plan, "dataRegion": o.DataRegion, "isolation": o.Isolation})
	c.JSON(http.StatusCreated, o)
}

func createProjectTx(tx *gorm.DB, org, slug, name string, now time.Time) error {
	if err := tx.Create(&store.Project{ID: iam.ULID(), OrgID: org, Slug: slug, Name: name, CreatedAt: now}).Error; err != nil {
		return err
	}
	for i, e := range defaultEnvs {
		if err := tx.Create(&store.Environment{ID: iam.ULID(), OrgID: org, Project: slug, Name: e, TargetRef: "compose", PromotionOrder: i + 1, CreatedAt: now}).Error; err != nil {
			return err
		}
	}
	return nil
}

func (s *Server) getOrg(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	var o store.Organization
	if err := s.DB.WithContext(c).First(&o, "slug = ?", org).Error; err != nil {
		fail(c, http.StatusNotFound, "org_not_found", "조직 없음", nil)
		return
	}
	c.JSON(http.StatusOK, o)
}

// patchOrg: 상태 전환(active ↔ suspended)·이름·요금제 — 플랫폼 관리자.
func (s *Server) patchOrg(c *gin.Context) {
	if !principal(c).PlatformAdmin {
		fail(c, http.StatusForbidden, "forbidden", "플랫폼 관리자만", nil)
		return
	}
	var r orgRequest
	_ = c.ShouldBindJSON(&r)
	var o store.Organization
	if err := s.DB.WithContext(c).First(&o, "slug = ?", c.Param("org")).Error; err != nil {
		fail(c, http.StatusNotFound, "org_not_found", "조직 없음", nil)
		return
	}
	before := o
	if r.Status != "" {
		if r.Status != "active" && r.Status != "suspended" {
			fail(c, http.StatusBadRequest, "bad_request", "status = active | suspended (삭제는 DELETE)", nil)
			return
		}
		o.Status = r.Status
		if r.Status == "active" {
			o.DeleteAfter = nil
		}
	}
	if r.Name != "" {
		o.Name = r.Name
	}
	if r.Plan != "" {
		o.Plan = r.Plan
	}
	o.UpdatedAt = time.Now().UTC()
	if err := s.DB.WithContext(c).Save(&o).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	s.audit(c, o.Slug, "org.update", orgObj(o.Slug), "success", before, o, map[string]any{"status": o.Status})
	if before.Status != o.Status {
		s.emit(c, "plt.org."+map[string]string{"suspended": "suspended", "active": "resumed"}[o.Status], gin.H{"org": o.Slug})
	}
	c.JSON(http.StatusOK, o)
}

// deleteOrg 는 30일 유예 삭제다: 상태 deleting + delete_after. 유예 중 PATCH status=active 로 복구.
func (s *Server) deleteOrg(c *gin.Context) {
	if !principal(c).PlatformAdmin {
		fail(c, http.StatusForbidden, "forbidden", "플랫폼 관리자만", nil)
		return
	}
	if c.Param("org") == "default" {
		fail(c, http.StatusConflict, "protected", "default 조직은 삭제할 수 없음", nil)
		return
	}
	var o store.Organization
	if err := s.DB.WithContext(c).First(&o, "slug = ?", c.Param("org")).Error; err != nil {
		fail(c, http.StatusNotFound, "org_not_found", "조직 없음", nil)
		return
	}
	at := time.Now().UTC().Add(30 * 24 * time.Hour)
	o.Status, o.DeleteAfter, o.UpdatedAt = "deleting", &at, time.Now().UTC()
	if err := s.DB.WithContext(c).Save(&o).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	s.audit(c, o.Slug, "org.delete", orgObj(o.Slug), "success", nil, o, map[string]any{"deleteAfter": at})
	s.emit(c, "plt.org.deleting", gin.H{"org": o.Slug, "deleteAfter": at})
	c.JSON(http.StatusAccepted, o)
}

// exportOrg 는 전체 내보내기 tar.gz 다(매니페스트·설정·감사로그, 비밀·API 키 해시 제외).
func (s *Server) exportOrg(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "admin", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	db := s.db(c)
	var o store.Organization
	if err := s.DB.WithContext(c).First(&o, "slug = ?", org).Error; err != nil {
		fail(c, http.StatusNotFound, "org_not_found", "조직 없음", nil)
		return
	}
	var projects []store.Project
	var envs []store.Environment
	var quotas []store.Quota
	var agents []store.Agent
	var audits []store.AuditEvent
	db.Where("org_id = ?", org).Find(&projects)
	db.Where("org_id = ?", org).Find(&envs)
	db.Where("org_id = ?", org).Find(&quotas)
	db.Where("org_id = ?", org).Find(&agents)
	db.Where("org_id = ?", org).Order("seq").Find(&audits)
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	add := func(name string, v any) {
		b, _ := json.MarshalIndent(v, "", "  ")
		_ = tw.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(b)), ModTime: time.Now()})
		_, _ = tw.Write(b)
	}
	add("organization.json", o)
	add("projects.json", projects)
	add("environments.json", envs)
	add("quotas.json", quotas)
	for _, a := range agents {
		var rev store.Revision
		if db.First(&rev, "agent_id = ? AND rev = ?", a.ID, a.CurrentRev).Error == nil {
			var m any
			_ = json.Unmarshal([]byte(rev.Manifest), &m)
			add(fmt.Sprintf("agents/%s/%s.json", a.Project, a.Name), m)
		}
	}
	var lines bytes.Buffer
	for _, e := range audits {
		b, _ := json.Marshal(e)
		lines.Write(append(b, '\n'))
	}
	_ = tw.WriteHeader(&tar.Header{Name: "audit.jsonl", Mode: 0o644, Size: int64(lines.Len()), ModTime: time.Now()})
	_, _ = tw.Write(lines.Bytes())
	add("README.json", gin.H{"exportedAt": time.Now().UTC(), "excluded": []string{"secrets", "api key hashes", "member credentials"}})
	_ = tw.Close()
	_ = gz.Close()
	s.audit(c, org, "org.export", orgObj(org), "success", nil, nil, map[string]any{"bytes": buf.Len(), "agents": len(agents)})
	c.Header("Content-Disposition", fmt.Sprintf(`attachment; filename="atlas-%s-export.tar.gz"`, org))
	c.Data(http.StatusOK, "application/gzip", buf.Bytes())
}

// ---- 프로젝트·환경 (FR-PLT-TEN-02) ----

func (s *Server) listProjects(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	var ps []store.Project
	if err := s.db(c).Where("org_id = ?", org).Order("slug").Find(&ps).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": ps})
}

func (s *Server) createProject(c *gin.Context) {
	org := c.Param("org")
	if _, ok := s.orgGuard(c, org); !ok || !s.require(c, "admin", orgObj(org)) {
		return
	}
	var r struct{ Slug, Name string }
	if err := c.ShouldBindJSON(&r); err != nil || !slugRe.MatchString(r.Slug) {
		fail(c, http.StatusBadRequest, "bad_request", "slug: 소문자·숫자·- 2~63자", nil)
		return
	}
	s.tenantScope(c, org)
	err := createProjectTx(s.db(c), org, r.Slug, firstNonEmpty(r.Name, r.Slug), time.Now().UTC())
	if errors.Is(err, gorm.ErrDuplicatedKey) {
		fail(c, http.StatusConflict, "already_exists", "같은 slug 의 프로젝트가 있음", nil)
		return
	}
	if err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	if s.FGA != nil {
		if err := s.FGA.Write(c, iam.Structure(org, r.Slug, defaultEnvs), nil); err != nil {
			fail(c, http.StatusBadGateway, "authz_backend", err.Error(), nil)
			return
		}
	}
	s.audit(c, org, "project.create", projObj(org, r.Slug), "success", nil, r, nil)
	c.JSON(http.StatusCreated, gin.H{"org": org, "slug": r.Slug, "envs": defaultEnvs})
}

func (s *Server) listEnvs(c *gin.Context) {
	org, proj := c.Param("org"), c.Param("proj")
	if !s.require(c, "viewer", projObj(org, proj)) {
		return
	}
	s.tenantScope(c, org)
	var es []store.Environment
	if err := s.db(c).Where("org_id = ? AND project = ?", org, proj).Order("promotion_order").Find(&es).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": es})
}

func (s *Server) createEnv(c *gin.Context) {
	org, proj := c.Param("org"), c.Param("proj")
	if _, ok := s.orgGuard(c, org); !ok || !s.require(c, "admin", projObj(org, proj)) {
		return
	}
	var r struct {
		Name           string `json:"name"`
		TargetRef      string `json:"targetRef"`
		PromotionOrder int    `json:"promotionOrder"`
	}
	if err := c.ShouldBindJSON(&r); err != nil || !slugRe.MatchString(r.Name) {
		fail(c, http.StatusBadRequest, "bad_request", "name: 소문자·숫자·- 2~63자", nil)
		return
	}
	s.tenantScope(c, org)
	db := s.db(c)
	var n int64
	db.Model(&store.Environment{}).Where("org_id = ? AND project = ?", org, proj).Count(&n)
	if n == 0 {
		fail(c, http.StatusNotFound, "project_not_found", "프로젝트 없음", nil)
		return
	}
	if n >= 10 {
		fail(c, http.StatusUnprocessableEntity, "env_limit", "프로젝트당 환경 최대 10개", nil)
		return
	}
	e := store.Environment{ID: iam.ULID(), OrgID: org, Project: proj, Name: r.Name, TargetRef: firstNonEmpty(r.TargetRef, "compose"),
		PromotionOrder: max(r.PromotionOrder, int(n)+1), CreatedAt: time.Now().UTC()}
	if err := db.Create(&e).Error; errors.Is(err, gorm.ErrDuplicatedKey) {
		fail(c, http.StatusConflict, "already_exists", "같은 이름의 환경이 있음", nil)
		return
	} else if err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	if s.FGA != nil {
		_ = s.FGA.Write(c, []iam.Tuple{{User: projObj(org, proj), Relation: "project", Object: envObj(org, proj, r.Name)}}, nil)
	}
	s.audit(c, org, "env.create", envObj(org, proj, r.Name), "success", nil, e, nil)
	c.JSON(http.StatusCreated, e)
}

// ---- 쿼터 (FR-PLT-TEN-03) ----

var quotaKeys = map[string]bool{"agents.max": true, "tasks.concurrent": true, "cpu.millicores": true, "storage.gb": true, "tokens.monthly": true}

func (s *Server) listQuotas(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	db := s.db(c)
	var qs []store.Quota
	db.Where("org_id = ?", org).Order("scope, scope_id, qkey").Find(&qs)
	type qv struct {
		store.Quota
		Used *int64 `json:"used,omitempty"`
	}
	out := make([]qv, 0, len(qs))
	for _, q := range qs {
		v := qv{Quota: q}
		if q.Qkey == "agents.max" {
			n := s.agentCount(db, org, q)
			v.Used = &n
		}
		out = append(out, v)
	}
	c.JSON(http.StatusOK, gin.H{"items": out})
}

func (s *Server) agentCount(db *gorm.DB, org string, q store.Quota) int64 {
	var n int64
	m := db.Model(&store.Agent{}).Where("org_id = ?", org)
	if q.Scope == "project" {
		m = m.Where("project = ?", strings.TrimPrefix(q.ScopeID, org+"/"))
	}
	m.Count(&n)
	return n
}

// putQuota: 조직 쿼터는 플랫폼 관리자, 프로젝트 쿼터는 조직 관리자. 하위 합 ≤ 상위.
func (s *Server) putQuota(c *gin.Context) {
	org := c.Param("org")
	var r struct {
		Scope   string `json:"scope"`
		Project string `json:"project"`
		Key     string `json:"key"`
		Limit   int    `json:"limit"`
	}
	if err := c.ShouldBindJSON(&r); err != nil || !quotaKeys[r.Key] || r.Limit < 0 || (r.Scope != "org" && r.Scope != "project") {
		fail(c, http.StatusBadRequest, "bad_request", "scope org|project · key agents.max|tasks.concurrent|cpu.millicores|storage.gb|tokens.monthly · limit ≥ 0", nil)
		return
	}
	if r.Scope == "org" && !principal(c).PlatformAdmin {
		fail(c, http.StatusForbidden, "forbidden", "조직 쿼터는 플랫폼 관리자만", nil)
		return
	}
	if r.Scope == "project" && !s.require(c, "admin", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	db := s.db(c)
	q := store.Quota{OrgID: org, Scope: r.Scope, ScopeID: org, Qkey: r.Key, Qlimit: r.Limit}
	if r.Scope == "project" {
		q.ScopeID = org + "/" + firstNonEmpty(r.Project, "default")
		var parent store.Quota
		if db.First(&parent, "scope = 'org' AND scope_id = ? AND qkey = ?", org, r.Key).Error == nil {
			var sum int64
			db.Model(&store.Quota{}).Where("scope = 'project' AND org_id = ? AND qkey = ? AND scope_id <> ?", org, r.Key, q.ScopeID).
				Select("COALESCE(SUM(qlimit), 0)").Scan(&sum)
			if sum+int64(r.Limit) > int64(parent.Qlimit) {
				fail(c, http.StatusUnprocessableEntity, "quota_exceeds_parent", fmt.Sprintf("프로젝트 합 %d > 조직 한도 %d", sum+int64(r.Limit), parent.Qlimit), nil)
				return
			}
		}
	}
	if err := db.Save(&q).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	s.audit(c, org, "quota.set", q.Scope+":"+q.ScopeID+":"+q.Qkey, "success", nil, q, nil)
	c.JSON(http.StatusOK, q)
}

// checkQuota 는 에이전트 생성 전 agents.max 를 본다. 초과면 429 + plt.quota.exceeded.
func (s *Server) checkQuota(c *gin.Context, db *gorm.DB, org, project string) bool {
	var qs []store.Quota
	db.Where("qkey = 'agents.max' AND ((scope = 'org' AND scope_id = ?) OR (scope = 'project' AND scope_id = ?))", org, org+"/"+project).Find(&qs)
	if _, l := s.effectiveLimits(c, org); l.MaxAgents > 0 { // 요금제 max_agents (FR-X-WSP-08) — 명시 조직 쿼터가 있으면 그 값
		qs = append(qs, store.Quota{OrgID: org, Scope: "org", ScopeID: org, Qkey: "agents.max", Qlimit: l.MaxAgents})
	}
	for _, q := range qs {
		used := s.agentCount(db, org, q)
		if q.Scope == "org" {
			s.threshold(c, org, "agents.max", "current", used+1, int64(q.Qlimit))
		}
		if used >= int64(q.Qlimit) {
			s.emit(c, "plt.quota.exceeded", gin.H{"org": org, "scope": q.Scope, "scopeId": q.ScopeID, "key": q.Qkey, "limit": q.Qlimit, "used": used})
			fail(c, http.StatusTooManyRequests, "quota_exceeded", fmt.Sprintf("%s %s 한도 %d 초과", q.Scope, q.Qkey, q.Qlimit), gin.H{"used": used})
			return false
		}
	}
	return true
}

// ---- 멤버·역할 (FR-PLT-IAM-04) ----

type memberRequest struct {
	User      string            `json:"user"` // Keycloak sub 또는 group:{name}#member
	Role      string            `json:"role"`
	Project   string            `json:"project"`
	Env       string            `json:"env"`
	Condition *iam.RelCondition `json:"condition,omitempty"` // ABAC 조건 (FR-PLT-IAM-05): business_hours·ip_allowlist·mfa_verified·amount_limit
}

// conditionalRoles 는 모델에서 조건부 직접 관계를 허용하는 (역할 → 조건) 이다.
var conditionalRoles = map[string][]string{"env-deployer": {"business_hours", "ip_allowlist", "mfa_verified"}, "billing": {"amount_limit", "business_hours"}}

func (s *Server) memberTuple(org string, r memberRequest) (iam.Tuple, error) {
	role, ok := iam.Roles[r.Role]
	if !ok || r.User == "" || r.Role == "platform-admin" {
		return iam.Tuple{}, fmt.Errorf("role 은 %s 중 하나 (platform-admin 은 Keycloak realm role)", strings.Join(roleNames(), " · "))
	}
	user := r.User
	if !strings.Contains(user, ":") {
		user = "user:" + user
	}
	obj := orgObj(org)
	switch role[0] {
	case "project":
		obj = projObj(org, firstNonEmpty(r.Project, "default"))
	case "environment":
		if r.Env == "" {
			return iam.Tuple{}, errors.New("env-deployer 는 env 필요")
		}
		obj = envObj(org, firstNonEmpty(r.Project, "default"), r.Env)
	}
	t := iam.Tuple{User: user, Relation: role[1], Object: obj}
	if r.Condition != nil {
		if !strings.HasPrefix(user, "user:") || !contains(conditionalRoles[r.Role], r.Condition.Name) {
			return iam.Tuple{}, fmt.Errorf("조건 %q 는 사용자 주체의 %v 역할에만 (env-deployer: business_hours·ip_allowlist·mfa_verified, billing: amount_limit·business_hours)", r.Condition.Name, r.Role)
		}
		t.Condition = r.Condition
	}
	return t, nil
}

func roleNames() []string {
	out := make([]string, 0, len(iam.Roles))
	for k := range iam.Roles {
		if k != "platform-admin" {
			out = append(out, k)
		}
	}
	return out
}

func (s *Server) listMembers(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "admin", orgObj(org)) {
		return
	}
	if s.FGA == nil {
		c.JSON(http.StatusOK, gin.H{"items": []any{}})
		return
	}
	var projects []store.Project
	s.DB.WithContext(c).Where("org_id = ?", org).Find(&projects)
	objs := []string{orgObj(org)}
	for _, p := range projects {
		objs = append(objs, projObj(org, p.Slug))
	}
	items := []iam.Tuple{}
	for _, o := range objs {
		ts, err := s.FGA.Read(c, o)
		if err != nil {
			fail(c, http.StatusBadGateway, "authz_backend", err.Error(), nil)
			return
		}
		for _, t := range ts {
			if strings.HasPrefix(t.User, "user:") || strings.HasPrefix(t.User, "group:") {
				items = append(items, t)
			}
		}
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func (s *Server) changeMember(c *gin.Context, add bool) {
	org := c.Param("org")
	if _, ok := s.orgGuard(c, org); !ok || !s.require(c, "admin", orgObj(org)) {
		return
	}
	var r memberRequest
	_ = c.ShouldBindJSON(&r)
	t, err := s.memberTuple(org, r)
	if err != nil {
		fail(c, http.StatusBadRequest, "bad_request", err.Error(), nil)
		return
	}
	if s.FGA == nil {
		fail(c, http.StatusServiceUnavailable, "authz_backend", "OpenFGA 미설정", nil)
		return
	}
	w, d := []iam.Tuple{t}, []iam.Tuple(nil)
	action := "member.add"
	if !add {
		w, d, action = nil, []iam.Tuple{t}, "member.remove"
	}
	if err := s.FGA.Write(c, w, d); err != nil {
		fail(c, http.StatusBadGateway, "authz_backend", err.Error(), nil)
		return
	}
	s.audit(c, org, action, t.Object, "success", nil, t, map[string]any{"role": r.Role})
	c.JSON(http.StatusOK, t)
}

func (s *Server) addMember(c *gin.Context)    { s.changeMember(c, true) }
func (s *Server) removeMember(c *gin.Context) { s.changeMember(c, false) }

// ---- API 키 (FR-PLT-IAM-06) ----

func (s *Server) listAPIKeys(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "admin", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	var ks []store.APIKey
	s.db(c).Where("org_id = ?", org).Order("created_at DESC").Find(&ks)
	c.JSON(http.StatusOK, gin.H{"items": ks})
}

func (s *Server) createAPIKey(c *gin.Context) {
	org := c.Param("org")
	if _, ok := s.orgGuard(c, org); !ok || !s.require(c, "admin", orgObj(org)) {
		return
	}
	var r struct {
		Name          string `json:"name"`
		Scopes        string `json:"scopes"`
		ExpiresInDays int    `json:"expiresInDays"`
	}
	_ = c.ShouldBindJSON(&r)
	scopes := iam.ParseScopes(r.Scopes)
	if r.Name == "" || len(scopes) == 0 || r.ExpiresInDays <= 0 || r.ExpiresInDays > 365 {
		fail(c, http.StatusBadRequest, "bad_request", "name·scopes 필수, expiresInDays 1~365 (범위·만료 필수)", nil)
		return
	}
	for _, sc := range scopes {
		if !iam.KnownScopes[sc] {
			fail(c, http.StatusBadRequest, "bad_request", "알 수 없는 scope: "+sc, nil)
			return
		}
	}
	if sliceHas(scopes, "admin") && !principal(c).PlatformAdmin {
		fail(c, http.StatusForbidden, "forbidden", "admin 범위 키는 플랫폼 관리자만", nil)
		return
	}
	plain, prefix, salt, hash := iam.NewAPIKey(firstNonEmpty(s.KeyEnv, s.Env))
	now := time.Now().UTC()
	k := store.APIKey{ID: iam.ULID(), OrgID: org, Prefix: prefix, Hash: hash, Salt: salt, Name: r.Name, Owner: firstNonEmpty(principal(c).Name, principal(c).Sub),
		Scopes: strings.Join(scopes, " "), ExpiresAt: now.Add(time.Duration(r.ExpiresInDays) * 24 * time.Hour), CreatedAt: now}
	s.tenantScope(c, org)
	if err := s.db(c).Create(&k).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	s.audit(c, org, "apikey.create", "apikey:"+k.ID, "success", nil, gin.H{"prefix": k.Prefix, "scopes": k.Scopes, "expiresAt": k.ExpiresAt}, nil)
	c.JSON(http.StatusCreated, gin.H{"key": plain, "apiKey": k, "note": "평문 키는 지금 한 번만 표시됩니다"})
}

func sliceHas(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}

func (s *Server) revokeAPIKey(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "admin", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	now := time.Now().UTC()
	res := s.db(c).Model(&store.APIKey{}).Where("id = ? AND org_id = ? AND revoked_at IS NULL", c.Param("id"), org).Update("revoked_at", now)
	if res.Error != nil || res.RowsAffected == 0 {
		fail(c, http.StatusNotFound, "not_found", "키 없음(또는 이미 폐기)", nil)
		return
	}
	s.audit(c, org, "apikey.revoke", "apikey:"+c.Param("id"), "success", nil, nil, nil)
	c.Status(http.StatusNoContent)
}

// BootstrapAPIKey 는 env 로 받은 평문 키(atl_…)를 upsert 한다 — 에이전트 자가 등록·MCP 도구용 서비스 키.
func BootstrapAPIKey(db *gorm.DB, plain, org, scopes string) (string, error) {
	prefix, secret, err := iam.SplitAPIKey(plain)
	if err != nil {
		return "", err
	}
	salt, hash := iam.HashSecret(secret)
	now := time.Now().UTC()
	var k store.APIKey
	if db.First(&k, "prefix = ?", prefix).Error != nil {
		k = store.APIKey{ID: iam.ULID(), Prefix: prefix, CreatedAt: now}
	}
	k.OrgID, k.Salt, k.Hash, k.Name, k.Owner, k.Scopes = org, salt, hash, "bootstrap (agents)", "system", scopes
	k.ExpiresAt, k.RevokedAt = now.Add(365*24*time.Hour), nil
	return prefix, db.Save(&k).Error
}

// ---- 감사 (FR-PLT-IAM-08) ----

func (s *Server) listAudit(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "auditor", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	q := s.db(c).Where("org_id = ?", org).Order("seq DESC")
	if a := c.Query("action"); a != "" {
		q = q.Where("action = ?", a)
	}
	limit, _ := strconv.Atoi(c.DefaultQuery("limit", "100"))
	var evs []store.AuditEvent
	q.Limit(min(max(limit, 1), 1000)).Find(&evs)
	c.JSON(http.StatusOK, gin.H{"items": evs})
}

func (s *Server) verifyAudit(c *gin.Context) {
	org := c.Param("org")
	// 검증 결과(유효·건수)만 주므로 같은 조직 에이전트 주체(MAT 성숙도 진단)도 허용 — 감사 내용 조회(listAudit)는 auditor 그대로
	if p := principal(c); !(p.Kind == "agent" && p.Org == org) && !s.require(c, "auditor", orgObj(org)) {
		return
	}
	n, broken, err := s.Audit.Verify(c, org)
	if err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	c.JSON(http.StatusOK, gin.H{"org": org, "events": n, "valid": broken == 0, "brokenAt": broken})
}

func (s *Server) auditRoot(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "auditor", orgObj(org)) {
		return
	}
	day, err := time.Parse("2006-01-02", c.DefaultQuery("date", time.Now().UTC().Format("2006-01-02")))
	if err != nil {
		fail(c, http.StatusBadRequest, "bad_request", "date=YYYY-MM-DD", nil)
		return
	}
	root, n, err := s.Audit.Root(c, org, day)
	if err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	c.JSON(http.StatusOK, gin.H{"org": org, "date": day.Format("2006-01-02"), "events": n, "merkleRoot": root})
}

func (s *Server) exportAudit(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "auditor", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	var evs []store.AuditEvent
	s.db(c).Where("org_id = ?", org).Order("seq").Find(&evs)
	var buf bytes.Buffer
	for _, e := range evs {
		b, _ := json.Marshal(e)
		buf.Write(append(b, '\n'))
	}
	c.Data(http.StatusOK, "application/jsonl", buf.Bytes())
}
