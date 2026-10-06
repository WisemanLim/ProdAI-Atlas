package api

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"atlas-manager/internal/iam"
	"atlas-manager/internal/store"
)

// 인증 모드: off(로컬 개발 — 모두 플랫폼 관리자) · oidc(Keycloak 토큰 또는 API 키 필수).
const (
	AuthOff  = "off"
	AuthOIDC = "oidc"
)

var publicPaths = map[string]bool{"/api/v1/openapi.json": true, "/api/v1/catalog/units": true, "/api/v1/auth/config": true}

func (s *Server) authOn() bool { return s.AuthMode == AuthOIDC }

func bearer(c *gin.Context) string {
	if after, ok := strings.CutPrefix(c.GetHeader("Authorization"), "Bearer "); ok {
		return strings.TrimSpace(after)
	}
	return ""
}

// authenticate 는 주체를 정한다 (FR-PLT-IAM-01·06).
func (s *Server) authenticate(c *gin.Context) {
	tok := bearer(c)
	if p, ok := s.agentPrincipal(tok); ok { // Manager 가 발급한 에이전트 단기 토큰 (비밀 중개, FR-PLT-SEC-08) — 모드 무관
		c.Set("principal", p)
		return
	}
	if p, ok := s.delegatedPrincipal(tok); ok { // Token Exchange 위임 토큰 (FR-PLT-IAM-07, RFC 8693)
		c.Set("principal", p)
		return
	}
	if !s.authOn() {
		c.Set("principal", iam.Principal{Kind: "anonymous", Sub: "dev", PlatformAdmin: true})
		return
	}
	if tok == "" {
		if publicPaths[c.FullPath()] {
			c.Set("principal", iam.Principal{Kind: "anonymous"})
			return
		}
		c.Header("WWW-Authenticate", `Bearer realm="atlas"`)
		fail(c, http.StatusUnauthorized, "unauthenticated", "Bearer 토큰(Keycloak) 또는 API 키 필요", nil)
		return
	}
	var p iam.Principal
	var err error
	if strings.HasPrefix(tok, "atl_") {
		p, err = s.apiKeyPrincipal(c, tok)
	} else if s.OIDC != nil {
		p, err = s.OIDC.Verify(c, tok)
	} else {
		err = errors.New("OIDC 미설정")
	}
	if err != nil {
		c.Header("WWW-Authenticate", `Bearer error="invalid_token"`)
		fail(c, http.StatusUnauthorized, "invalid_token", err.Error(), nil)
		return
	}
	c.Set("principal", p)
	if code, msg := s.checkSession(c, p); code != "" { // 세션 정책·SCIM 비활성 (FR-PLT-IAM-03·09)
		c.Header("WWW-Authenticate", `Bearer error="invalid_token", error_description="`+code+`"`)
		fail(c, http.StatusUnauthorized, code, msg, nil)
	}
}

func (s *Server) agentPrincipal(tok string) (iam.Principal, bool) {
	if s.Signer == nil || tok == "" || strings.HasPrefix(tok, "atl_") {
		return iam.Principal{}, false
	}
	c, err := s.Signer.Parse(tok)
	if err != nil || !strings.HasPrefix(c.Subject, "agent:") || len(c.Audience) == 0 || c.Audience[0] != agentAudience {
		return iam.Principal{}, false
	}
	return iam.Principal{Kind: "agent", Sub: c.Subject, Org: c.Org}, true
}

func (s *Server) apiKeyPrincipal(c *gin.Context, plain string) (iam.Principal, error) {
	prefix, secret, err := iam.SplitAPIKey(plain)
	if err != nil {
		return iam.Principal{}, err
	}
	var k store.APIKey
	if err := s.DB.First(&k, "prefix = ?", prefix).Error; err != nil {
		return iam.Principal{}, iam.ErrBadKey
	}
	now := time.Now().UTC()
	switch {
	case k.RevokedAt != nil:
		return iam.Principal{}, errors.New("폐기된 API 키")
	case now.After(k.ExpiresAt):
		return iam.Principal{}, errors.New("만료된 API 키")
	case !iam.VerifyAPIKey(secret, k.Salt, k.Hash):
		return iam.Principal{}, iam.ErrBadKey
	}
	go s.DB.Model(&store.APIKey{}).Where("id = ?", k.ID).Update("last_used_at", now) // 마지막 사용 시각
	return iam.Principal{Kind: "apikey", Sub: "apikey:" + k.ID, Name: k.Name, Org: k.OrgID, Scopes: iam.ParseScopes(k.Scopes), KeyID: k.ID}, nil
}

func principal(c *gin.Context) iam.Principal {
	if v, ok := c.Get("principal"); ok {
		return v.(iam.Principal)
	}
	return iam.Principal{Kind: "anonymous"}
}

// orgOf 는 요청의 조직 문맥이다: ?org= > X-Atlas-Org > 토큰 atlas_org > default.
func orgOf(c *gin.Context) string {
	for _, v := range []string{c.Query("org"), c.GetHeader("X-Atlas-Org"), principal(c).Org} {
		if v != "" {
			return v
		}
	}
	return "default"
}

// tenantScope 는 요청 트랜잭션에 RLS 문맥을 건다. 플랫폼 관리자의 조직 횡단 조회는 '*'.
func (s *Server) tenantScope(c *gin.Context, org string) {
	c.Set("tenant", org)
}

// db 는 요청 DB 핸들이다. PostgreSQL + 인증 모드면 첫 사용 시 트랜잭션을 열고
// `SET LOCAL ROLE atlas_rls` + `atlas.tenant` 를 설정한다(행 수준 보안, FR-PLT-TEN-05). 응답 후 commit/rollback.
func (s *Server) db(c *gin.Context) *gorm.DB {
	if tx, ok := c.Get("tx"); ok {
		return tx.(*gorm.DB)
	}
	if s.Engine != "postgres" || !s.authOn() {
		return s.DB.WithContext(c)
	}
	tenant := c.GetString("tenant")
	if tenant == "" {
		tenant = orgOf(c)
	}
	tx := s.DB.WithContext(c.Request.Context()).Begin()
	if err := tx.Exec("SET LOCAL ROLE atlas_rls").Error; err == nil {
		err = tx.Exec("SELECT set_config('atlas.tenant', ?, true)", tenant).Error
	}
	c.Set("tx", tx)
	return tx
}

// finishTx 는 요청 트랜잭션을 마무리한다(스트리밍 프록시는 DB 조회 후 직접 호출).
func (s *Server) finishTx(c *gin.Context) {
	v, ok := c.Get("tx")
	if !ok {
		return
	}
	tx := v.(*gorm.DB)
	if c.Writer.Status() < 400 && len(c.Errors) == 0 {
		tx.Commit()
	} else {
		tx.Rollback()
	}
	c.Set("tx", s.DB.WithContext(c)) // 이후 사용은 트랜잭션 밖
}

func (s *Server) txMiddleware(c *gin.Context) {
	c.Next()
	s.finishTx(c)
}

// scopeFor 는 relation → API 키 범위다.
var scopeFor = map[string]string{"viewer": "read", "developer": "agents:write", "deploy": "deployments:write", "invoke": "a2a:invoke",
	"auditor": "audit:read", "operator": "svc", "admin": "admin", "owner": "admin", "member": "read"}

// can 은 인가 검사다 (FR-PLT-IAM-04): off 모드·플랫폼 관리자 = 허용, API 키 = 조직 + 범위, 사용자 = OpenFGA Check.
func (s *Server) can(c *gin.Context, relation, object string) bool {
	p := principal(c)
	if !s.authOn() || p.PlatformAdmin {
		return true
	}
	switch p.Kind {
	case "agent": // 에이전트 단기 토큰: 같은 조직 안에서 조회·위임만 (MA-ORCH → 서브에이전트, FR-A2A-CLI-02)
		return orgFromObject(object) == p.Org && (relation == "viewer" || relation == "invoke")
	case "apikey", "delegated": // 위임 토큰 = 축소 범위의 API 키처럼 (조직 + 범위)
		org := strings.SplitN(strings.SplitN(object, ":", 2)[1], "/", 2)[0]
		return org == p.Org && p.HasScope(scopeFor[relation])
	case "user", "service":
		if s.FGA == nil {
			return false
		}
		ok, err := s.FGA.CheckCtx(c, p.FGAUser(), relation, object, abacContext(c)) // ABAC 조건 문맥 (FR-PLT-IAM-05)
		return err == nil && ok
	}
	return false
}

// require 는 can 이 거짓이면 403 + 감사(denied) 를 남기고 false.
func (s *Server) require(c *gin.Context, relation, object string) bool {
	if s.can(c, relation, object) {
		return s.requireMFA(c, relation, object)
	}
	if c.Request.Method != http.MethodGet {
		s.audit(c, orgFromObject(object), "authz.denied", object, "denied", nil, nil, map[string]any{"relation": relation})
	}
	fail(c, http.StatusForbidden, "forbidden", "권한 없음: "+relation+" on "+object, nil)
	return false
}

func orgFromObject(o string) string {
	_, id, _ := strings.Cut(o, ":")
	org, _, _ := strings.Cut(id, "/")
	return org
}

func orgObj(org string) string          { return "organization:" + org }
func projObj(org, proj string) string   { return "project:" + org + "/" + proj }
func envObj(org, proj, e string) string { return "environment:" + org + "/" + proj + "/" + e }

// audit 은 감사 이벤트를 남긴다(실패해도 응답을 막지 않는다).
func (s *Server) audit(c *gin.Context, org, action, resource, result string, before, after any, detail map[string]any) {
	if s.Audit == nil {
		return
	}
	p := principal(c)
	ev, err := s.Audit.Append(c, iam.Entry{Org: org, ActorType: p.Kind, ActorID: firstNonEmpty(p.Name, p.Sub), ActorIP: c.ClientIP(),
		Action: action, Resource: resource, Result: result, Before: before, After: after, Detail: detail})
	if err != nil {
		c.Error(err) //nolint:errcheck // 로그용
		return
	}
	// 감사 스트림 (FR-X-RPT-04·05): X-RPT 통합 감사 저장소·SIEM 으로 — 요청 경로를 막지 않게 비동기
	if s.Events != nil {
		go s.emit(context.Background(), "audit.recorded", gin.H{"org": ev.OrgID, "id": ev.ID, "seq": ev.Seq, "ts": ev.TS, "actorType": ev.ActorType,
			"actorId": ev.ActorID, "actorIp": ev.ActorIP, "action": ev.Action, "resource": ev.Resource, "result": ev.Result,
			"beforeHash": ev.BeforeHash, "afterHash": ev.AfterHash, "detail": ev.Detail, "hash": ev.Hash})
	}
}

func firstNonEmpty(v ...string) string {
	for _, s := range v {
		if s != "" {
			return s
		}
	}
	return ""
}

// orgGuard 는 조직 존재·상태 검사다 (FR-PLT-TEN-01): 정지된 조직은 플랫폼 관리자 외 403.
func (s *Server) orgGuard(c *gin.Context, org string) (store.Organization, bool) {
	var o store.Organization
	if err := s.DB.WithContext(c).First(&o, "slug = ?", org).Error; err != nil {
		fail(c, http.StatusNotFound, "org_not_found", "조직 없음: "+org, nil)
		return o, false
	}
	if o.Status != "active" && !principal(c).PlatformAdmin {
		fail(c, http.StatusForbidden, "org_"+o.Status, "조직 상태: "+o.Status, nil)
		return o, false
	}
	return o, true
}
