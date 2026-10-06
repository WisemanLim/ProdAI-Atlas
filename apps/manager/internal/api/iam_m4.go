package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"atlas-manager/internal/events"
	"atlas-manager/internal/iam"
	"atlas-manager/internal/store"
)

// 엔터프라이즈 IAM 잔여 (FR-PLT-IAM-05·07·09 · PLT-TEN-04 · X-WSP-08).

// ---- 세션 정책·MFA (FR-PLT-IAM-09) ----

func (s *Server) security(c context.Context, org string) *store.OrgSecurity {
	var sp store.OrgSecurity
	if s.DB.WithContext(c).Where("org_id = ?", org).Limit(1).Find(&sp).RowsAffected == 0 { // Find: 없음 = 정상 (로그 소음 없음)
		return nil
	}
	return &sp
}

// checkSession 은 OIDC 사용자 토큰의 세션 상태를 본다: SCIM 비활성·폐기 · 최대 수명 · 유휴 · 동시 세션.
func (s *Server) checkSession(c *gin.Context, p iam.Principal) (string, string) {
	if p.Kind != "user" {
		return "", ""
	}
	org, now := firstNonEmpty(c.Param("org"), orgOf(c)), time.Now().UTC() // 경로 조직 우선 (/orgs/{org}/…)
	db := s.DB.WithContext(c)
	var n int64
	db.Model(&store.ScimUser{}).Where("org_id = ? AND active = ? AND (external_id = ? OR user_name = ?)", org, false, p.Sub, firstNonEmpty(p.Name, p.Sub)).Count(&n)
	if n > 0 {
		return "user_deactivated", "디렉터리(SCIM)에서 비활성화된 사용자"
	}
	var rv store.SessionRevocation
	if db.Where("sub = ?", p.Sub).Limit(1).Find(&rv).RowsAffected > 0 && !p.IssuedAt.After(rv.RevokedAt) {
		return "session_revoked", "폐기 이전에 발급된 토큰"
	}
	sp := s.security(c, org)
	if sp == nil {
		return "", ""
	}
	if sp.MaxS > 0 && now.Sub(p.AuthTime) > time.Duration(sp.MaxS)*time.Second {
		return "session_expired", fmt.Sprintf("세션 최대 수명 %ds 초과 — 다시 로그인", sp.MaxS)
	}
	if p.Sid == "" {
		return "", ""
	}
	var as store.AuthSession
	if db.Where("sid = ?", p.Sid).Limit(1).Find(&as).RowsAffected > 0 {
		switch {
		case as.Revoked:
			return "session_revoked", "폐기된 세션"
		case sp.IdleS > 0 && now.Sub(as.LastSeen) > time.Duration(sp.IdleS)*time.Second:
			db.Model(&as).Update("revoked", true)
			return "session_idle", fmt.Sprintf("유휴 %ds 초과 — 다시 로그인", sp.IdleS)
		case now.Sub(as.LastSeen) > time.Second:
			db.Model(&as).Update("last_seen", now)
		}
		return "", ""
	}
	db.Create(&store.AuthSession{Sid: p.Sid, OrgID: org, Sub: p.Sub, CreatedAt: now, LastSeen: now})
	if sp.MaxSessions > 0 { // 동시 세션 상한 — 오래된 세션부터 폐기
		var live []store.AuthSession
		db.Where("sub = ? AND revoked = ?", p.Sub, false).Order("created_at DESC, sid").Find(&live)
		for _, old := range live[min(len(live), sp.MaxSessions):] {
			db.Model(&old).Update("revoked", true)
			s.emit(c, "plt.iam.session_revoked", gin.H{"org": org, "sub": p.Sub, "sid": old.Sid, "reason": "max_sessions"})
		}
	}
	return "", ""
}

// requireMFA: 조직 정책이 관리 작업 MFA 를 요구하면 사용자 토큰 acr 를 본다 (RFC 9470 step-up).
func (s *Server) requireMFA(c *gin.Context, relation, object string) bool {
	p := principal(c)
	if !s.authOn() || p.Kind != "user" || (relation != "admin" && relation != "owner") {
		return true
	}
	sp := s.security(c, orgFromObject(object))
	if sp == nil || !sp.MfaAdmin || p.Acr >= sp.AcrMin {
		return true
	}
	c.Header("WWW-Authenticate", fmt.Sprintf(`Bearer error="insufficient_user_authentication", error_description="관리 작업은 MFA 필요", acr_values="%d"`, sp.AcrMin))
	fail(c, http.StatusUnauthorized, "mfa_required", fmt.Sprintf("관리 작업은 MFA(acr ≥ %d) 필요 — 현재 acr %d", sp.AcrMin, p.Acr), gin.H{"acrValues": sp.AcrMin})
	return false
}

func (s *Server) getSecurityPolicy(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	sp := s.security(c, org)
	if sp == nil {
		sp = &store.OrgSecurity{OrgID: org, AcrMin: 2}
	}
	c.JSON(http.StatusOK, sp)
}

func (s *Server) putSecurityPolicy(c *gin.Context) {
	org := c.Param("org")
	if _, ok := s.orgGuard(c, org); !ok || !s.require(c, "admin", orgObj(org)) {
		return
	}
	var r store.OrgSecurity
	if err := c.ShouldBindJSON(&r); err != nil || r.IdleS < 0 || r.MaxS < 0 || r.MaxSessions < 0 || r.AcrMin < 0 || r.AcrMin > 4 {
		fail(c, http.StatusBadRequest, "bad_request", "mfaAdmin · acrMin 0~4 · idleS·maxS·maxSessions ≥ 0 (0 = 제한 없음)", nil)
		return
	}
	if r.AcrMin == 0 {
		r.AcrMin = 2
	}
	r.OrgID, r.UpdatedAt = org, time.Now().UTC()
	s.tenantScope(c, org)
	if err := s.db(c).Save(&r).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	s.audit(c, org, "security_policy.set", "org:"+org, "success", nil, r, nil)
	c.JSON(http.StatusOK, r)
}

// ---- ABAC 요청 문맥 (FR-PLT-IAM-05) ----

// abacContext 는 OpenFGA 조건 매개변수다: now·ip·mfa·amount(금액이 없는 작업은 0).
func abacContext(c *gin.Context) map[string]any {
	amount := 0.0
	if v, ok := c.Get("abac.amount"); ok {
		amount, _ = v.(float64)
	}
	return map[string]any{"now": time.Now().UTC().Format(time.RFC3339), "ip": c.ClientIP(), "mfa": principal(c).Acr >= 2, "amount": amount}
}

// authCheck 는 호출자 자신의 권한 미리보기다(콘솔 버튼 활성·조건 확인): GET /auth/check?relation=&object=&amount=
func (s *Server) authCheck(c *gin.Context) {
	rel, obj := c.Query("relation"), c.Query("object")
	if rel == "" || !strings.Contains(obj, ":") {
		fail(c, http.StatusBadRequest, "bad_request", "relation · object(type:id) 필요", nil)
		return
	}
	if a := c.Query("amount"); a != "" {
		if f, ok := new(big.Rat).SetString(a); ok {
			v, _ := f.Float64()
			c.Set("abac.amount", v)
		}
	}
	c.JSON(http.StatusOK, gin.H{"allowed": s.can(c, rel, obj), "relation": rel, "object": obj, "context": abacContext(c)})
}

// ---- 워크로드 신원·위임 (FR-PLT-IAM-07) ----

const tokenExchangeGrant = "urn:ietf:params:oauth:grant-type:token-exchange"

// delegatedPrincipal 은 Token Exchange 로 축소된 위임 토큰(act 클레임)을 주체로 만든다.
func (s *Server) delegatedPrincipal(tok string) (iam.Principal, bool) {
	if s.Signer == nil || tok == "" || strings.HasPrefix(tok, "atl_") {
		return iam.Principal{}, false
	}
	cl, err := s.Signer.Parse(tok)
	if err != nil || cl.Act == nil || len(cl.Audience) == 0 || cl.Audience[0] != agentAudience {
		return iam.Principal{}, false
	}
	return iam.Principal{Kind: "delegated", Sub: cl.Subject, Org: cl.Org, Scopes: iam.ParseScopes(cl.Scope), Actor: cl.Act.Sub}, true
}

// peerAudience 는 에이전트 토큰이 겨눌 수 있는 대상이다: Manager · MCP 게이트웨이 · 같은 조직 에이전트(agent:{id}) · MCP 서버(mcp:{name}).
func (s *Server) peerAudience(c *gin.Context, org, aud string) bool {
	switch {
	case aud == agentAudience || aud == GatewayAudience:
		return true
	case strings.HasPrefix(aud, "mcp:") && len(aud) > 4:
		return true
	case strings.HasPrefix(aud, "agent:"):
		var n int64
		s.DB.WithContext(c).Model(&store.Agent{}).Where("id = ? AND org_id = ?", strings.TrimPrefix(aud, "agent:"), org).Count(&n)
		return n > 0
	}
	return false
}

// tokenExchange: RFC 8693. 행위자 = 호출 에이전트(자기 단기 토큰), 주체 = 사용자(OIDC·API 키) 토큰.
// 범위는 주체 권한 ∩ 요청 범위 ∩ {read, a2a:invoke}(사용자) — 위임 토큰은 원래 권한보다 넓어질 수 없다.
func (s *Server) tokenExchange(c *gin.Context) {
	tfail := func(status int, code, desc string) {
		c.JSON(status, gin.H{"error": code, "error_description": desc})
	}
	actor := principal(c)
	if actor.Kind != "agent" || s.Signer == nil {
		tfail(http.StatusUnauthorized, "invalid_client", "행위자 = 에이전트 단기 토큰(Authorization) 필요")
		return
	}
	get := func(k string) string {
		if v := c.PostForm(k); v != "" {
			return v
		}
		return c.Query(k)
	}
	if get("grant_type") != tokenExchangeGrant || get("subject_token") == "" {
		tfail(http.StatusBadRequest, "invalid_request", "grant_type="+tokenExchangeGrant+" · subject_token 필요")
		return
	}
	subjTok := get("subject_token")
	var subj iam.Principal
	var err error
	allowed := map[string]bool{"read": true, "a2a:invoke": true}
	switch {
	case strings.HasPrefix(subjTok, "atl_"):
		subj, err = s.apiKeyPrincipal(c, subjTok)
		if err == nil {
			allowed = map[string]bool{}
			for _, sc := range subj.Scopes {
				if sc == "admin" {
					for k := range iam.KnownScopes {
						allowed[k] = true
					}
				}
				allowed[sc] = true
			}
		}
	case s.OIDC != nil:
		subj, err = s.OIDC.Verify(c, subjTok)
		if err == nil {
			if code, msg := s.checkSession(c, subj); code != "" {
				err = fmt.Errorf("%s: %s", code, msg)
			}
		}
	default:
		err = fmt.Errorf("OIDC 미설정 — subject_token 은 API 키만")
	}
	if err != nil {
		tfail(http.StatusBadRequest, "invalid_grant", "subject_token: "+err.Error())
		return
	}
	if subj.Org != "" && subj.Org != actor.Org {
		tfail(http.StatusForbidden, "invalid_target", "주체와 행위자 조직이 다름")
		return
	}
	req := iam.ParseScopes(get("scope"))
	if len(req) == 0 {
		req = []string{"read"}
	}
	for _, sc := range req {
		if !allowed[sc] {
			tfail(http.StatusBadRequest, "invalid_scope", "주체 권한 밖 범위: "+sc)
			return
		}
	}
	aud := firstNonEmpty(get("audience"), agentAudience)
	if !s.peerAudience(c, actor.Org, aud) {
		tfail(http.StatusBadRequest, "invalid_target", "audience = "+agentAudience+" | "+GatewayAudience+" | agent:{id} | mcp:{name}")
		return
	}
	scope := strings.Join(req, " ")
	tok, err := s.Signer.MintClaims(iam.TenantClaims{Org: actor.Org, Act: &iam.Actor{Sub: actor.Sub}, Scope: scope,
		RegisteredClaims: jwt.RegisteredClaims{Subject: subj.Sub, Audience: jwt.ClaimStrings{aud}}}, 5*time.Minute)
	if err != nil {
		tfail(http.StatusInternalServerError, "server_error", err.Error())
		return
	}
	s.audit(c, actor.Org, "token.exchange", "subject:"+subj.Sub, "success", nil, nil, map[string]any{"actor": actor.Sub, "aud": aud, "scope": scope})
	c.JSON(http.StatusOK, gin.H{"access_token": tok, "issued_token_type": "urn:ietf:params:oauth:token-type:jwt", "token_type": "Bearer",
		"expires_in": 300, "scope": scope})
}

// ---- 예산 상속 (FR-PLT-TEN-04): 조직 → 프로젝트 → 환경, 하위 합 ≤ 상위, 집행 = Gateway 계층 예산 ----

type budgetBody struct {
	Scope          string `json:"scope"`
	Project        string `json:"project"`
	Env            string `json:"env"`
	Period         string `json:"period"`
	AmountUSD      string `json:"amountUsd"`
	SoftPct        int    `json:"softPct"`
	HardAction     string `json:"hardAction"`
	DowngradeAlias string `json:"downgradeAlias"`
}

func rat(v string) *big.Rat {
	r, ok := new(big.Rat).SetString(v)
	if !ok {
		return new(big.Rat)
	}
	return r
}

// decStr 는 정규화 decimal(소수 8자리까지, 뒤 0 제거)이다 — 1e-8 USD 단위 Gateway 정산과 맞춘다.
func decStr(r *big.Rat) string {
	v := strings.TrimRight(r.FloatString(8), "0")
	return strings.TrimSuffix(v, ".")
}

func budgetParent(b store.Budget) (string, string) {
	switch b.Scope {
	case "project":
		return "org", b.OrgID
	case "env":
		return "project", b.ScopeID[:strings.LastIndex(b.ScopeID, "/")]
	}
	return "", ""
}

func childScope(scope string) string {
	return map[string]string{"org": "project", "project": "env"}[scope]
}

func childOf(child, parent store.Budget) bool {
	return child.Scope == childScope(parent.Scope) && strings.HasPrefix(child.ScopeID, parent.ScopeID+"/") &&
		!strings.Contains(strings.TrimPrefix(child.ScopeID, parent.ScopeID+"/"), "/")
}

// gwCall 은 Gateway 관리 API 호출이다(관리 토큰 = 비밀 저장소 default/gateway-admin-token).
func (s *Server) gwCall(ctx context.Context, method, path string, body any) (int, []byte, error) {
	u := s.Services["gw"]
	if u == nil {
		return 0, nil, fmt.Errorf("gateway 주소 미설정 (ATLAS_SVC_URLS gw=)")
	}
	tok := os.Getenv("ATLAS_GATEWAY_ADMIN_TOKEN")
	if s.Secrets != nil {
		if sec, err := s.Secrets.Get(ctx, secretPath("default", "gateway-admin-token")); err == nil && sec.Value != "" {
			tok = sec.Value
		}
	}
	var r io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		r = bytes.NewReader(b)
	}
	req, _ := http.NewRequestWithContext(ctx, method, strings.TrimRight(u.String(), "/")+path, r)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+tok)
	res, err := s.Proxy.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(res.Body, 1<<20))
	return res.StatusCode, raw, nil
}

func gwBudgetID(b store.Budget) string { return "mgr." + b.OrgID + "." + b.ID }

func (s *Server) listBudgetsM(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	var bs []store.Budget
	s.db(c).Where("org_id = ?", org).Order("scope_id, period").Find(&bs)
	status := map[string]map[string]any{}
	if code, raw, err := s.gwCall(c, http.MethodGet, "/admin/budgets", nil); err == nil && code == http.StatusOK {
		var out struct {
			Data []map[string]any `json:"data"`
		}
		_ = json.Unmarshal(raw, &out)
		for _, d := range out.Data {
			if id, _ := d["id"].(string); id != "" {
				status[id] = d
			}
		}
	}
	items := make([]gin.H, 0, len(bs))
	for _, b := range bs {
		it := gin.H{"budget": b}
		if st := status[gwBudgetID(b)]; st != nil {
			it["spentUsd"], it["pct"], it["state"], it["windowStart"] = st["spent_usd"], st["pct"], st["state"], st["window_start"]
		}
		items = append(items, it)
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func (s *Server) putBudgetM(c *gin.Context) {
	org, id := c.Param("org"), c.Param("id")
	var r budgetBody
	_ = c.ShouldBindJSON(&r)
	amt, ok := new(big.Rat).SetString(r.AmountUSD)
	if !ok || amt.Sign() <= 0 || (r.Scope != "org" && r.Scope != "project" && r.Scope != "env") || (r.Scope == "env" && r.Env == "") {
		fail(c, http.StatusBadRequest, "bad_request", "scope org|project|env(env 필요) · amountUsd 양수 decimal", nil)
		return
	}
	f, _ := amt.Float64()
	c.Set("abac.amount", f) // 금액 조건 (amount_limit) — 큰 예산 변경은 한도 있는 billing 으로 막는다
	if _, ok := s.orgGuard(c, org); !ok || !s.require(c, "billing", orgObj(org)) {
		return
	}
	b := store.Budget{ID: id, OrgID: org, Scope: r.Scope, Period: firstNonEmpty(r.Period, "monthly"), AmountUSD: decStr(amt),
		SoftPct: r.SoftPct, HardAction: firstNonEmpty(r.HardAction, "block"), DowngradeAlias: optStr(r.DowngradeAlias), CreatedAt: time.Now().UTC()}
	if b.SoftPct <= 0 || b.SoftPct >= 100 {
		b.SoftPct = 80
	}
	switch b.Scope {
	case "org":
		b.ScopeID = org
	case "project":
		b.ScopeID = org + "/" + firstNonEmpty(r.Project, defaultProject)
	case "env":
		b.ScopeID = org + "/" + firstNonEmpty(r.Project, defaultProject) + "/" + r.Env
	}
	if b.HardAction != "block" && b.HardAction != "downgrade" && b.HardAction != "notify" {
		fail(c, http.StatusBadRequest, "bad_request", "hardAction = block | downgrade | notify", nil)
		return
	}
	s.tenantScope(c, org)
	db := s.db(c)
	var all []store.Budget
	db.Where("org_id = ? AND period = ? AND id <> ?", org, b.Period, id).Find(&all)
	// 상위 한도: 같은 상위 아래 형제 합 + 이번 금액 ≤ 상위
	if ps, pid := budgetParent(b); ps != "" {
		for _, p := range all {
			if p.Scope == ps && p.ScopeID == pid {
				sum := new(big.Rat).Set(amt)
				for _, sib := range all {
					if childOf(sib, p) {
						sum.Add(sum, rat(sib.AmountUSD))
					}
				}
				if sum.Cmp(rat(p.AmountUSD)) > 0 {
					fail(c, http.StatusUnprocessableEntity, "budget_exceeds_parent", fmt.Sprintf("하위 합 %s > 상위(%s %s) %s", decStr(sum), p.Scope, p.ScopeID, p.AmountUSD),
						gin.H{"parent": p.ID})
					return
				}
			}
		}
	}
	// 하위 합 ≤ 이번 금액
	children := new(big.Rat)
	for _, ch := range all {
		if childOf(ch, b) {
			children.Add(children, rat(ch.AmountUSD))
		}
	}
	if children.Cmp(amt) > 0 {
		fail(c, http.StatusUnprocessableEntity, "budget_below_children", fmt.Sprintf("하위 예산 합 %s > %s", decStr(children), b.AmountUSD), nil)
		return
	}
	gb := gin.H{"id": gwBudgetID(b), "scope": b.Scope, "scope_id": b.ScopeID, "amount_usd": b.AmountUSD, "period": b.Period, "soft_pct": b.SoftPct,
		"hard_action": map[string]string{"notify": "alert"}[b.HardAction], "tz": "UTC"}
	if gb["hard_action"] == "" {
		gb["hard_action"] = b.HardAction
	}
	if b.Scope == "org" {
		gb["scope_id"] = org // Gateway org 범위 = 가상키 tenant(조직 slug)
	}
	if b.DowngradeAlias != nil {
		gb["downgrade_alias"] = *b.DowngradeAlias
	}
	code, raw, err := s.gwCall(c, http.MethodPut, "/admin/budgets/"+gwBudgetID(b), gb)
	if err != nil || code >= 300 {
		fail(c, http.StatusBadGateway, "gateway_budget_failed", fmt.Sprintf("Gateway 예산 반영 실패: %v %s", err, raw), nil)
		return
	}
	if err := db.Save(&b).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	s.audit(c, org, "budget.set", "budget:"+b.ID, "success", nil, b, nil)
	c.JSON(http.StatusOK, b)
}

func (s *Server) deleteBudgetM(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "billing", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	var b store.Budget
	if s.db(c).First(&b, "id = ? AND org_id = ?", c.Param("id"), org).Error != nil {
		fail(c, http.StatusNotFound, "not_found", "예산 없음", nil)
		return
	}
	_, _, _ = s.gwCall(c, http.MethodDelete, "/admin/budgets/"+gwBudgetID(b), nil)
	s.db(c).Delete(&b)
	s.audit(c, org, "budget.delete", "budget:"+b.ID, "success", b, nil, nil)
	c.Status(http.StatusNoContent)
}

// ---- 요금제·워크스페이스 할당량 (FR-X-WSP-08) ----

// PlanLimits 는 요금제 한도다(0 = 무제한).
type PlanLimits struct {
	MaxAgents      int   `json:"max_agents"`
	MaxConcurrency int   `json:"max_concurrency"`
	StorageGB      int   `json:"storage_gb"`
	MonthlyTokens  int64 `json:"monthly_tokens"`
}

func (s *Server) listPlans(c *gin.Context) {
	var ps []store.Plan
	s.DB.WithContext(c).Order("id").Find(&ps)
	items := make([]gin.H, 0, len(ps))
	for _, p := range ps {
		var l PlanLimits
		_ = json.Unmarshal([]byte(p.Limits), &l)
		items = append(items, gin.H{"id": p.ID, "name": p.Name, "limits": l, "updatedAt": p.UpdatedAt})
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func (s *Server) putPlan(c *gin.Context) {
	if !principal(c).PlatformAdmin {
		fail(c, http.StatusForbidden, "forbidden", "요금제는 플랫폼 관리자만", nil)
		return
	}
	var r struct {
		Name   string     `json:"name"`
		Limits PlanLimits `json:"limits"`
	}
	if err := c.ShouldBindJSON(&r); err != nil || r.Limits.MaxAgents < 0 || r.Limits.MaxConcurrency < 0 || r.Limits.StorageGB < 0 || r.Limits.MonthlyTokens < 0 {
		fail(c, http.StatusBadRequest, "bad_request", "limits{max_agents,max_concurrency,storage_gb,monthly_tokens} ≥ 0", nil)
		return
	}
	p := store.Plan{ID: c.Param("id"), Name: firstNonEmpty(r.Name, c.Param("id")), Limits: mustJSON(r.Limits), UpdatedAt: time.Now().UTC()}
	if err := s.DB.WithContext(c).Save(&p).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	s.audit(c, "default", "plan.set", "plan:"+p.ID, "success", nil, r, nil)
	c.JSON(http.StatusOK, gin.H{"id": p.ID, "name": p.Name, "limits": r.Limits})
}

// effectiveLimits 는 조직 요금제 한도에 명시 조직 쿼터(agents.max·tasks.concurrent·storage.gb·tokens.monthly)를 덮어쓴 값이다.
func (s *Server) effectiveLimits(c context.Context, org string) (string, PlanLimits) {
	var o store.Organization
	var l PlanLimits
	db := s.DB.WithContext(c)
	if db.Where("slug = ?", org).Limit(1).Find(&o).RowsAffected == 0 {
		return "", l
	}
	var p store.Plan
	if db.Where("id = ?", o.Plan).Limit(1).Find(&p).RowsAffected > 0 {
		_ = json.Unmarshal([]byte(p.Limits), &l)
	}
	var qs []store.Quota
	db.Where("scope = 'org' AND scope_id = ?", org).Find(&qs)
	for _, q := range qs {
		switch q.Qkey {
		case "agents.max":
			l.MaxAgents = q.Qlimit
		case "tasks.concurrent":
			l.MaxConcurrency = q.Qlimit
		case "storage.gb":
			l.StorageGB = q.Qlimit
		case "tokens.monthly":
			l.MonthlyTokens = int64(q.Qlimit)
		}
	}
	return o.Plan, l
}

var inflight sync.Map // org → *atomic.Int64 (단일 Manager 인스턴스 기준 동시 실행 수)

func orgInflight(org string) *atomic.Int64 {
	v, _ := inflight.LoadOrStore(org, new(atomic.Int64))
	return v.(*atomic.Int64)
}

func month(t time.Time) string { return t.UTC().Format("2006-01") }

func (s *Server) monthTokens(c context.Context, org string) int64 {
	var u store.UsageMonthly
	s.DB.WithContext(c).Where("org_id = ? AND month = ?", org, month(time.Now())).Limit(1).Find(&u)
	return u.Tokens
}

// threshold 는 80%·100% 도달을 기간당 1회만 plt.quota.threshold 로 낸다 (→ X-NTF).
func (s *Server) threshold(c context.Context, org, key, period string, used, limit int64) {
	if limit <= 0 {
		return
	}
	for _, lv := range []int{80, 100} {
		if used*100 < limit*int64(lv) {
			continue
		}
		res := s.DB.WithContext(c).Clauses(clause.OnConflict{DoNothing: true}).Create(&store.QuotaMark{OrgID: org, Qkey: key, Period: period, Level: lv, At: time.Now().UTC()})
		if res.Error == nil && res.RowsAffected > 0 {
			s.emit(c, "plt.quota.threshold", gin.H{"org": org, "key": key, "level": lv, "used": used, "limit": limit, "period": period})
		}
	}
}

// admitRun 은 실행(A2A) 전 할당량 게이트다: 월 토큰 · 동시 실행. 통과하면 해제 함수를 돌려준다.
func (s *Server) admitRun(c *gin.Context, org string) (func(), bool) {
	_, l := s.effectiveLimits(c, org)
	if l.MonthlyTokens > 0 {
		if used := s.monthTokens(c, org); used >= l.MonthlyTokens {
			s.emit(c, "plt.quota.exceeded", gin.H{"org": org, "scope": "org", "key": "tokens.monthly", "limit": l.MonthlyTokens, "used": used})
			fail(c, http.StatusTooManyRequests, "quota_exceeded", fmt.Sprintf("월 토큰 한도 %d 초과", l.MonthlyTokens), gin.H{"used": used, "key": "tokens.monthly"})
			return nil, false
		}
	}
	ctr := orgInflight(org)
	n := ctr.Add(1)
	if l.MaxConcurrency > 0 {
		if n > int64(l.MaxConcurrency) {
			ctr.Add(-1)
			s.emit(c, "plt.quota.exceeded", gin.H{"org": org, "scope": "org", "key": "tasks.concurrent", "limit": l.MaxConcurrency, "used": n - 1})
			c.Header("Retry-After", "1")
			fail(c, http.StatusTooManyRequests, "quota_exceeded", fmt.Sprintf("동시 실행 한도 %d 초과", l.MaxConcurrency), gin.H{"used": n - 1, "key": "tasks.concurrent"})
			return nil, false
		}
		s.threshold(c, org, "tasks.concurrent", time.Now().UTC().Format("2006-01-02T15:04"), n, int64(l.MaxConcurrency))
	}
	return func() { ctr.Add(-1) }, true
}

func (s *Server) orgUsage(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	plan, l := s.effectiveLimits(c, org)
	var agents int64
	s.DB.WithContext(c).Model(&store.Agent{}).Where("org_id = ?", org).Count(&agents)
	tokens := s.monthTokens(c, org)
	pct := func(used, limit int64) *int64 {
		if limit <= 0 {
			return nil
		}
		v := used * 100 / limit
		return &v
	}
	conc := orgInflight(org).Load()
	c.JSON(http.StatusOK, gin.H{"org": org, "plan": plan, "limits": l, "month": month(time.Now()),
		"used": gin.H{"agents": agents, "concurrency": conc, "monthlyTokens": tokens},
		"pct":  gin.H{"agents": pct(agents, int64(l.MaxAgents)), "concurrency": pct(conc, int64(l.MaxConcurrency)), "monthlyTokens": pct(tokens, l.MonthlyTokens)}})
}

// ConsumeUsage 는 Gateway gw.request.completed 를 조직·월 토큰 사용량으로 누적한다(할당량·임계 이벤트 원천).
func (s *Server) ConsumeUsage(ctx context.Context, js *events.JetStream) {
	js.Consume(ctx, "GW", "atlas-manager-usage", func(subject string, ev events.Event) {
		if subject != "gw.request.completed" {
			return
		}
		raw, _ := json.Marshal(ev.Data)
		var d struct {
			Tenant string `json:"tenant"`
			Input  int64  `json:"input"`
			Output int64  `json:"output"`
		}
		if json.Unmarshal(raw, &d) != nil || d.Tenant == "" || d.Input+d.Output <= 0 {
			return
		}
		if err := s.addUsage(ctx, d.Tenant, d.Input+d.Output); err != nil {
			slog.Warn("usage accumulate failed", "err", err)
		}
	})
}

func (s *Server) addUsage(ctx context.Context, org string, tokens int64) error {
	m := month(time.Now())
	err := s.DB.WithContext(ctx).Clauses(clause.OnConflict{Columns: []clause.Column{{Name: "org_id"}, {Name: "month"}},
		DoUpdates: clause.Assignments(map[string]any{"tokens": gorm.Expr("usage_monthly.tokens + ?", tokens)})}).
		Create(&store.UsageMonthly{OrgID: org, Month: m, Tokens: tokens}).Error
	if err != nil {
		return err
	}
	if _, l := s.effectiveLimits(ctx, org); l.MonthlyTokens > 0 {
		s.threshold(ctx, org, "tokens.monthly", m, s.monthTokens(ctx, org), l.MonthlyTokens)
	}
	return nil
}
