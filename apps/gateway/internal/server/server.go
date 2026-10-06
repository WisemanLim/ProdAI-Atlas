// Package server 는 게이트웨이 HTTP 표면이다: OpenAI 호환 데이터 플레인(/v1) + 관리 API(/admin) — PRD §17.3.
// 요청 파이프라인(PRD §8.7): Auth → Policy → Rate → Route(+Key) → Cost → Logs/Audit → 응답.
package server

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"

	"atlas-gateway/internal/aliases"
	"atlas-gateway/internal/audit"
	"atlas-gateway/internal/auth"
	"atlas-gateway/internal/capture"
	"atlas-gateway/internal/config"
	"atlas-gateway/internal/cost"
	"atlas-gateway/internal/econ"
	"atlas-gateway/internal/events"
	"atlas-gateway/internal/guard"
	"atlas-gateway/internal/keyrot"
	"atlas-gateway/internal/provider"
	"atlas-gateway/internal/ratelimit"
	"atlas-gateway/internal/router"
)

//go:embed openapi.json
var openapiSpec []byte

type Server struct {
	Cfg        *config.Config
	Redis      *redis.Client
	Keys       *auth.Store
	Limiter    *ratelimit.Limiter
	Routes     *router.Router
	Prices     *cost.Table
	Audit      *audit.Logger
	Events     events.Publisher
	Pools      map[string]*provider.KeyPool // provider → 업스트림 키 풀
	AdminToken string
	Env        string
	Now        func() time.Time
	Guard      *guard.Client // nil 이면 가드레일 훅 없음
	Capture    string        // span 본문 기록 정책 none | metadata | redacted | full (FR-OB-LOG-01)
	Version    string
	Budgets    *econ.Budgets   // nil 이면 계층 예산 없음 (EC-CTRL-01)
	Tasks      *econ.Tasks     // nil 이면 과업 토큰 예산 없음 (EC-CTRL-03)
	Batches    *econ.Batches   // nil 이면 /v1/batches 503
	KeyRot     *keyrot.Manager // nil 이면 키 회전 API 503 (비밀 저장소 미설정)
	Aliases    *aliases.Store  // 런타임 alias 오버레이·서빙 통계 (BL-MODEL) — nil 이면 alias 관리 API 503
	Compute    *econ.Compute   // 계산 정책 (BL-COMP) — nil 이면 정책 없음
}

// tagKeys 는 x-atlas-tags 화이트리스트다 (FR-GW-COST-04 카디널리티 통제).
var tagKeys = map[string]bool{"agent_id": true, "unit_id": true, "tenant": true, "app": true, "feature": true, "customer": true, "trace": true, "eval_run": true, "task_type": true}

func (s *Server) Router() *gin.Engine {
	r := gin.New()
	r.Use(gin.Recovery())
	r.GET("/health", s.health)
	r.GET("/openapi.json", func(c *gin.Context) { c.Data(http.StatusOK, "application/json", openapiSpec) })
	v1 := r.Group("/v1")
	v1.POST("/chat/completions", s.chat)
	v1.GET("/models", s.models)
	v1.POST("/embeddings", s.embeddings)
	v1.POST("/econ/simulate", s.simulate)
	v1.POST("/batches", s.createBatch)
	v1.GET("/batches", s.listBatches)
	v1.GET("/batches/:id", s.getBatch)
	v1.GET("/batches/:id/output", s.batchOutput)
	v1.POST("/batches/:id/retry", s.retryBatch)
	adm := r.Group("/admin", s.requireAdmin)
	adm.POST("/keys", s.issueKey)
	adm.GET("/keys", s.listKeys)
	adm.DELETE("/keys/:prefix", s.revokeKey)
	adm.GET("/usage", s.usage)
	adm.GET("/upstream-keys", s.upstreamKeys)
	adm.POST("/upstream-keys/:provider/:index/compromise", s.compromiseKey)
	adm.POST("/upstream-keys/:provider/rotate", s.rotateKey)
	adm.GET("/upstream-keys/:provider/rotation", s.rotationStatus)
	adm.GET("/circuits", func(c *gin.Context) { c.JSON(http.StatusOK, s.Routes.CircuitStates()) })
	adm.GET("/econ/models", s.catalog)
	adm.GET("/econ/cache", s.cacheKPI)
	adm.DELETE("/econ/cache/exact", s.purgeExact)
	adm.GET("/econ/tasks/:id", s.taskUsage)
	adm.GET("/aliases", s.listAliases)
	adm.GET("/aliases/:name", s.getAlias)
	adm.PUT("/aliases/:name", s.putAlias)
	adm.DELETE("/aliases/:name", s.deleteAlias)
	adm.GET("/aliases/:name/history", s.aliasHistory)
	adm.POST("/aliases/:name/rollback", s.rollbackAlias)
	adm.GET("/aliases/:name/stats", s.aliasStats)
	adm.GET("/compute-policies", s.listCompute)
	adm.PUT("/compute-policies/:id", s.putCompute)
	adm.DELETE("/compute-policies/:id", s.deleteCompute)
	adm.GET("/compute-policies/:id/usage", s.computeUsage)
	adm.GET("/budgets", s.listBudgets)
	adm.PUT("/budgets/:id", s.putBudget)
	adm.DELETE("/budgets/:id", s.deleteBudget)
	return r
}

func (s *Server) health(c *gin.Context) {
	st, code := "ok", http.StatusOK
	if err := s.Redis.Ping(c).Err(); err != nil {
		st, code = "error", http.StatusServiceUnavailable
	}
	c.JSON(code, gin.H{"status": map[bool]string{true: "ok", false: "degraded"}[code == 200], "env": s.Env, "redis": st,
		"aliases": len(s.Cfg.Aliases), "version": s.Version, "component": "atlas-gateway"})
}

// oaiError 는 OpenAI 형식 오류 응답이다.
func oaiError(c *gin.Context, status int, typ, code, msg string) {
	c.AbortWithStatusJSON(status, gin.H{"error": gin.H{"message": msg, "type": typ, "code": code}})
}

func bearer(c *gin.Context) string {
	h := c.GetHeader("Authorization")
	if after, ok := strings.CutPrefix(h, "Bearer "); ok {
		return strings.TrimSpace(after)
	}
	return ""
}

func (s *Server) requireAdmin(c *gin.Context) {
	if s.AdminToken == "" || bearer(c) != s.AdminToken {
		oaiError(c, http.StatusUnauthorized, "authentication_error", "invalid_admin_token", "관리 토큰 필요")
	}
}

func (s *Server) authenticate(c *gin.Context) (auth.VirtualKey, bool) {
	vk, err := s.Keys.Lookup(c, bearer(c))
	switch {
	case errors.Is(err, auth.ErrInvalid):
		oaiError(c, http.StatusUnauthorized, "authentication_error", "invalid_api_key", "유효하지 않은 API 키")
	case errors.Is(err, auth.ErrExpired):
		oaiError(c, http.StatusUnauthorized, "authentication_error", "expired_api_key", "만료된 API 키")
	case err != nil:
		oaiError(c, http.StatusServiceUnavailable, "api_error", "auth_backend_unavailable", err.Error())
	default:
		return vk, true
	}
	return vk, false
}

type chatRequest struct {
	Model          string             `json:"model"`
	Messages       []provider.Message `json:"messages"`
	MaxTokens      *int               `json:"max_tokens,omitempty"`
	Temperature    *float64           `json:"temperature,omitempty"`
	Stream         bool               `json:"stream,omitempty"`
	ResponseFormat *struct {
		Type string `json:"type"`
	} `json:"response_format,omitempty"`
	Metadata struct {
		Tags []string `json:"tags,omitempty"`
	} `json:"metadata"`
	Tools      []json.RawMessage `json:"tools,omitempty"`
	ToolChoice json.RawMessage   `json:"tool_choice,omitempty"`
	Atlas      struct {
		Effort   string `json:"effort"`    // minimal | low | medium | high (EC-MODEL-02)
		TaskType string `json:"task_type"` // classify · extract · summarize · code · reasoning (EC-MODEL-01)
	} `json:"atlas"`
}

func lastUser(ms []provider.Message) string {
	for i := len(ms) - 1; i >= 0; i-- {
		if ms[i].Role == "user" {
			return ms[i].Content
		}
	}
	return ""
}

func firstNonEmpty(v ...string) string {
	for _, s := range v {
		if s != "" {
			return s
		}
	}
	return ""
}

func parseTags(header string, body []string) map[string]string {
	out := map[string]string{}
	for _, kv := range append(strings.Split(header, ","), body...) {
		k, v, ok := strings.Cut(strings.TrimSpace(kv), ":")
		if ok && tagKeys[k] && v != "" && len(v) <= 128 {
			out[k] = v
		}
	}
	return out
}

var tracer = otel.Tracer("atlas-gateway")

func (s *Server) chat(c *gin.Context) {
	start := s.Now()
	reqID := "chatcmpl-" + uuid.NewString()
	c.Header("x-atlas-request-id", reqID)
	// OB-TRACE-06: 호출자(에이전트 SDK)의 traceparent 를 이어받아 같은 trace 에 chat span 을 만든다.
	ctx := otel.GetTextMapPropagator().Extract(c.Request.Context(), propagation.HeaderCarrier(c.Request.Header))
	ctx, span := tracer.Start(ctx, "chat", trace.WithSpanKind(trace.SpanKindServer),
		trace.WithAttributes(attribute.String("gen_ai.operation.name", "chat"), attribute.String("atlas.request_id", reqID)))
	defer span.End()
	c.Request = c.Request.WithContext(ctx)
	traceID := ""
	if sc := span.SpanContext(); sc.IsValid() {
		traceID = sc.TraceID().String()
		c.Header("x-atlas-trace-id", traceID)
	}

	// 1) Auth (GW-AUTH)
	vk, ok := s.authenticate(c)
	if !ok {
		return
	}
	raw, err := io.ReadAll(io.LimitReader(c.Request.Body, 4<<20))
	ev := audit.Event{TS: start.UTC(), Actor: vk.Prefix, Tenant: vk.Tenant, Action: "chat.completions",
		RequestHash: audit.RequestHash(raw), Extra: map[string]string{"request_id": reqID, "trace_id": traceID}}
	span.SetAttributes(attribute.String("atlas.tenant", vk.Tenant), attribute.String("atlas.key_prefix", vk.Prefix))
	deny := func(status int, typ, code, msg string) {
		ev.Decision, ev.Reasons = "deny", []string{code}
		s.Audit.Record(ev)
		span.SetStatus(codes.Error, code)
		span.SetAttributes(attribute.String("error.type", code), attribute.Int("http.response.status_code", status))
		oaiError(c, status, typ, code, msg)
	}
	if err != nil {
		deny(http.StatusBadRequest, "invalid_request_error", "bad_body", err.Error())
		return
	}
	var generic map[string]any
	var req chatRequest
	if json.Unmarshal(raw, &generic) != nil || json.Unmarshal(raw, &req) != nil {
		deny(http.StatusBadRequest, "invalid_request_error", "invalid_json", "JSON 본문 필요")
		return
	}
	ev.Resource = req.Model
	if req.Model == "" || len(req.Messages) == 0 {
		deny(http.StatusBadRequest, "invalid_request_error", "missing_field", "model·messages 필수")
		return
	}
	tags := parseTags(c.GetHeader("x-atlas-tags"), req.Metadata.Tags)
	tags["trace"] = traceID
	span.SetName("chat " + req.Model)
	span.SetAttributes(attribute.String("gen_ai.request.model", req.Model))
	if u := tags["unit_id"]; u != "" {
		span.SetAttributes(attribute.String("atlas.unit_id", u))
	}

	// 2) Policy (GW-POL-01 · GW-AUTH-05)
	for _, p := range s.Cfg.Policy.DeniedParams {
		if _, has := generic[p]; has {
			deny(http.StatusBadRequest, "invalid_request_error", "param_not_allowed", "정책상 금지된 파라미터: "+p)
			return
		}
	}
	if !s.Routes.Known(req.Model) && req.Model != "auto" { // auto = 과업 정책이 alias 선택 (EC-MODEL-01) · 오버레이 alias 포함
		deny(http.StatusNotFound, "invalid_request_error", "model_not_found", "알 수 없는 모델 alias: "+req.Model)
		return
	}
	if req.Model != "auto" && !vk.Allows(req.Model) {
		deny(http.StatusForbidden, "permission_error", "model_not_allowed", "이 키는 "+req.Model+" 를 호출할 수 없음")
		return
	}
	// 2-0) 경제성 정책 (EC-ROUTE-01 · EC-MODEL-01·02 · EC-CTRL-02)
	taskType := firstNonEmpty(c.GetHeader("x-atlas-task-type"), req.Atlas.TaskType, tags["task_type"])
	effortIn := firstNonEmpty(c.GetHeader("x-atlas-effort"), req.Atlas.Effort)
	inTok := provider.EstimateTokens(provider.PromptText(req.Messages))
	// 2-0-0) 계산 정책 (BL-COMP-01·02·05): 정책 effort(auto = 난이도) · max output · 추론 예산 소진 시 한 단계 하향
	var cd econ.ComputeDecision
	if s.Compute != nil {
		if d, ok := s.Compute.Decide(c, econ.ComputeMatch{Agent: firstNonEmpty(tags["unit_id"], tags["agent_id"]), TaskType: taskType, Tenant: vk.Tenant, Alias: req.Model},
			effortIn, lastUser(req.Messages), inTok, len(req.Tools)); ok {
			cd, effortIn = d, d.Effort
			if req.MaxTokens == nil && d.MaxOutput > 0 {
				mo := d.MaxOutput
				req.MaxTokens = &mo
			}
			c.Header("x-atlas-compute-policy", d.Policy)
			if d.Difficulty != "" {
				c.Header("x-atlas-difficulty", d.Difficulty)
			}
			if d.Downgraded != "" {
				c.Header("x-atlas-compute-downgraded", d.Downgraded)
			}
			span.SetAttributes(attribute.String("atlas.compute.policy", d.Policy), attribute.String("atlas.compute.difficulty", d.Difficulty))
		}
	}
	plan := econ.Plan(s.Cfg, econ.PlanInput{Alias: req.Model, TaskType: taskType, Tags: tags,
		InputTokens: inTok, HasTools: len(req.Tools) > 0, MaxTokens: req.MaxTokens,
		Effort: effortIn, Pinned: c.GetHeader("x-atlas-route-pin") == "true", Allows: vk.Allows})
	if cd.Thinking > 0 && plan.Effort != "" {
		plan.ThinkingBudget = cd.Thinking
	}
	alias, maxTokens, mutations := plan.Alias, plan.MaxTokens, plan.Mutations
	if !vk.Allows(alias) {
		deny(http.StatusForbidden, "permission_error", "model_not_allowed", "이 키는 "+alias+" 를 호출할 수 없음")
		return
	}
	if plan.Rule != "" {
		c.Header("x-atlas-econ-route", plan.Rule)
	}
	if plan.Effort != "" {
		c.Header("x-atlas-effort", plan.Effort)
	}
	if taskType != "" {
		span.SetAttributes(attribute.String("atlas.task_type", taskType))
	}

	// 2-0-1) 계층 예산 (EC-CTRL-01) · 과업 토큰 예산 (EC-CTRL-03)
	scopes := econ.Scopes{Org: vk.Tenant, Project: vk.Project, Env: vk.Metadata["env"], Key: vk.Prefix}
	var warnings []string
	if s.Budgets != nil {
		bv, berr := s.Budgets.Check(c, scopes)
		if berr != nil {
			oaiError(c, http.StatusServiceUnavailable, "api_error", "budget_backend_unavailable", berr.Error())
			return
		}
		warnings = bv.Warnings
		switch {
		case bv.Action == "block" || (bv.Action == "downgrade" && !vk.Allows(bv.Alias)):
			c.Header("x-atlas-budget-warning", strings.Join(warnings, "; "))
			deny(http.StatusTooManyRequests, "rate_limit_error", "budget_exceeded", "예산 초과: "+bv.Budget)
			return
		case bv.Action == "downgrade" && bv.Alias != alias:
			mutations = append(mutations, fmt.Sprintf("budget_downgrade:%s:%s->%s", bv.Budget, alias, bv.Alias))
			alias = bv.Alias
		}
	}
	taskID := firstNonEmpty(c.GetHeader("x-atlas-task-id"), traceID)
	taskLimit := 0
	if s.Tasks != nil && taskID != "" {
		taskLimit = s.Tasks.Limit(c.GetHeader("x-atlas-task-budget"))
		if used := s.Tasks.Used(c, taskID); taskLimit > 0 {
			c.Header("x-atlas-task-tokens", fmt.Sprintf("%d/%d", used, taskLimit))
			if used >= taskLimit {
				deny(http.StatusTooManyRequests, "rate_limit_error", "task_budget_exceeded", fmt.Sprintf("과업 토큰 예산 소진 %d/%d", used, taskLimit))
				return
			}
			if used*100 >= taskLimit*s.Tasks.SoftPct {
				warnings = append(warnings, fmt.Sprintf("task:%s %d%%", taskID, used*100/taskLimit))
			}
		}
	}
	if len(warnings) > 0 {
		c.Header("x-atlas-budget-warning", strings.Join(warnings, "; "))
	}
	if len(mutations) > 0 {
		c.Header("x-atlas-policy-mutations", strings.Join(mutations, ";"))
	}
	ev.Extra["alias_planned"] = alias

	// 2-1) 가드레일 pre_call — user 메시지 입력 경계 (FR-GW-POL-02 · GR-IO)
	if s.Guard != nil {
		var idx []int
		var texts []string
		for i, m := range req.Messages {
			if m.Role == "user" && m.Content != "" {
				idx, texts = append(idx, i), append(texts, m.Content)
			}
		}
		if len(texts) > 0 {
			gv, gerr := s.Guard.Check(ctx, vk.Tenant, "input", reqID, texts)
			c.Header("x-atlas-guard-input", gv.Decision)
			span.AddEvent("atlas.guardrail.decision", trace.WithAttributes(attribute.String("boundary", "input"),
				attribute.String("decision", gv.Decision), attribute.String("verdict_id", gv.ID), attribute.StringSlice("categories", gv.Categories)))
			span.SetAttributes(attribute.String("guard.input.decision", gv.Decision))
			ev.Extra["guard_input"] = gv.Decision
			switch {
			case gerr != nil && !s.Guard.FailOpen:
				deny(http.StatusServiceUnavailable, "api_error", "guard_unavailable", "가드레일 서비스 응답 없음 (fail_closed)")
				return
			case gv.Decision == guard.Block:
				c.Header("x-atlas-guard-verdict", gv.ID)
				deny(http.StatusBadRequest, "invalid_request_error", "guard_blocked", "가드레일 차단: "+strings.Join(gv.Reasons, "; "))
				return
			case gv.Decision == guard.Transform && len(gv.Contents) == len(idx):
				for k, i := range idx {
					req.Messages[i].Content = gv.Contents[k]
				}
				mutations = append(mutations, "guard:input")
			}
		}
	}

	// 3) Rate limits (GW-RATE-01·02)
	rd, err := s.Limiter.AllowRequest(c, vk.Prefix, vk.RPM, start)
	if err != nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "ratelimit_backend_unavailable", err.Error())
		return
	}
	if rd.Remaining >= 0 {
		c.Header("x-ratelimit-remaining-requests", strconv.Itoa(rd.Remaining))
	}
	if !rd.Allowed {
		c.Header("Retry-After", strconv.Itoa(int(rd.RetryAfter.Seconds())+1))
		deny(http.StatusTooManyRequests, "rate_limit_error", "rate_limit_exceeded", "RPM 한도 초과")
		return
	}
	est := provider.EstimateTokens(provider.PromptText(req.Messages)) + maxTokens
	td, err := s.Limiter.ReserveTokens(c, vk.Prefix, vk.TPM, est, start)
	if err != nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "ratelimit_backend_unavailable", err.Error())
		return
	}
	if td.Remaining >= 0 {
		c.Header("x-ratelimit-remaining-tokens", strconv.Itoa(max(0, td.Remaining)))
	}
	if !td.Allowed {
		c.Header("Retry-After", strconv.Itoa(int(td.RetryAfter.Seconds())))
		deny(http.StatusTooManyRequests, "rate_limit_error", "tpm_exceeded", "TPM 한도 초과")
		return
	}

	// 3-1) exact 응답 캐시 (EC-CACHE-04) — temperature 0 만
	xcOK, xcState := econ.ExactEligible(s.Cfg.Econ.ExactCache.Enabled, generic, c.GetHeader("Cache-Control"))
	xcKey := ""
	if xcOK {
		xcKey = econ.ExactKey(vk.Tenant, alias, plan.Effort, req.Messages, generic)
		if hit, _ := econ.GetExact(c, s.Redis, xcKey); hit != nil {
			_ = s.Limiter.Settle(c, vk.Prefix, vk.TPM, est, 0, start)
			s.serveExact(c, reqID, vk, req, alias, hit, ev, start, tags, generic, span)
			return
		}
		xcState = "MISS"
	}
	c.Header("x-atlas-cache", xcState)

	// 3-2) 공급사 prompt cache 지시 (EC-CACHE-01)
	pcKey, pcTTL := econ.PromptCacheKey(vk.Tenant, alias, req.Messages, len(req.Tools), s.Cfg.Econ.PromptCache.MinTokens), ""
	if pcKey != "" {
		pcTTL = econ.CacheTTL(c, s.Redis, pcKey, start)
		c.Header("x-atlas-prompt-cache", pcTTL)
	}

	// 4) Route (GW-ROUTE · GW-KEY)
	preq := provider.Request{Messages: req.Messages, MaxTokens: maxTokens, Temperature: req.Temperature, Tools: req.Tools, ToolChoice: req.ToolChoice,
		CacheKey: pcKey, CacheTTL: pcTTL, Effort: plan.Effort, ThinkingBudget: plan.ThinkingBudget,
		AssignKey: firstNonEmpty(c.GetHeader("x-atlas-assign-key"), tags["customer"])}
	if sd, ok := generic["seed"].(float64); ok {
		seed := int(sd)
		preq.Seed = &seed
	}
	if req.ResponseFormat != nil {
		preq.ResponseFormat = req.ResponseFormat.Type
	}
	rctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	res, err := s.Routes.Route(rctx, alias, preq)
	if s.Aliases != nil { // alias 버전 × 모델 서빙 통계 (카나리 판단, BL-MODEL-05)
		al, _ := s.Routes.Alias(alias)
		model := res.Model
		if model == "" && len(res.Attempts) > 0 {
			model = res.Attempts[len(res.Attempts)-1].Model
		}
		var ce int64
		if err == nil {
			u0 := res.Response.Usage
			ce = cost.E8(s.Prices.CostCached(res.Model, u0.Prompt, u0.Cached, u0.CacheWrite, u0.Completion, start).USD)
		}
		go s.Aliases.Record(context.WithoutCancel(ctx), alias, al.Version, model, err == nil, ce, s.Now().Sub(start))
		c.Header("x-atlas-alias-version", strconv.Itoa(al.Version))
	}
	if err != nil {
		span.SetStatus(codes.Error, "upstream_error")
		span.SetAttributes(attribute.String("error.type", "upstream_error"))
		_ = s.Limiter.Settle(c, vk.Prefix, vk.TPM, est, 0, start)
		ev.Decision, ev.Reasons = "error", []string{err.Error()}
		s.Audit.Record(ev)
		s.emitCompleted(c, reqID, vk, alias, res, nil, "", start, "error", tags)
		var pe *provider.Error
		status := http.StatusBadGateway
		if errors.As(err, &pe) && pe.Status == http.StatusTooManyRequests {
			status = http.StatusTooManyRequests
		}
		oaiError(c, status, "api_error", "upstream_error", err.Error())
		return
	}

	// 4-1) 가드레일 post_call — 출력 경계 (차단 시 content_filter 로 치환, 치환 시 마스킹본 반환)
	if s.Guard != nil && res.Response.Content != "" && len(res.Response.ToolCalls) == 0 {
		gv, gerr := s.Guard.Check(ctx, vk.Tenant, "output", reqID, []string{res.Response.Content})
		c.Header("x-atlas-guard-output", gv.Decision)
		span.AddEvent("atlas.guardrail.decision", trace.WithAttributes(attribute.String("boundary", "output"),
			attribute.String("decision", gv.Decision), attribute.String("verdict_id", gv.ID), attribute.StringSlice("categories", gv.Categories)))
		span.SetAttributes(attribute.String("guard.output.decision", gv.Decision))
		ev.Extra["guard_output"] = gv.Decision
		switch {
		case (gerr != nil && !s.Guard.FailOpen) || gv.Decision == guard.Block:
			res.Response.Content, res.Response.FinishReason = "", "content_filter"
		case gv.Decision == guard.Transform && len(gv.Contents) == 1:
			res.Response.Content = gv.Contents[0]
		}
	}

	// 5) Cost (GW-COST-01·03·04)
	u := res.Response.Usage
	bd := s.Prices.CostCached(res.Model, u.Prompt, u.Cached, u.CacheWrite, u.Completion, start)
	usd, priced := bd.USD, bd.Known
	costStr := cost.Format(usd)
	_ = s.Limiter.Settle(c, vk.Prefix, vk.TPM, est, u.Prompt+u.Completion, start)
	s.accumulate(c, vk, res.Alias, u, usd)
	c.Header("x-atlas-cost-usd", costStr)
	c.Header("x-atlas-route", res.Provider+"/"+res.Model)
	if u.Reasoning > 0 { // 추론 토큰 별도 계측 (BL-COMP-05)
		span.SetAttributes(attribute.Int("gen_ai.usage.reasoning.output_tokens", u.Reasoning))
		c.Header("x-atlas-reasoning-tokens", strconv.Itoa(u.Reasoning))
		if s.Compute != nil {
			s.Compute.RecordReasoning(c, cd.Policy, u.Reasoning)
		}
	}
	if res.Alias != alias {
		c.Header("x-atlas-fallback", res.Alias)
	}
	// 5-1) 경제성 정산: 예산 · 과업 토큰 · 캐시 KPI · exact 캐시 저장
	if s.Budgets != nil {
		s.Budgets.Settle(c, scopes, cost.E8(usd))
	}
	if s.Tasks != nil && taskID != "" {
		used := s.Tasks.Add(c, taskID, u.Prompt+u.Completion, taskLimit, vk.Tenant)
		if taskLimit > 0 {
			c.Header("x-atlas-task-tokens", fmt.Sprintf("%d/%d", used, taskLimit))
		}
	}
	if pcKey != "" || u.Cached > 0 || u.CacheWrite > 0 {
		econ.RecordPrompt(c, s.Redis, vk.Tenant, res.Alias, u, cost.E8(bd.Savings), map[bool]string{true: pcTTL, false: "5m"}[pcTTL != ""])
		c.Header("x-atlas-cache-read-tokens", strconv.Itoa(u.Cached))
		c.Header("x-atlas-cache-savings-usd", cost.Format(bd.Savings))
		span.SetAttributes(attribute.Int("gen_ai.usage.cache_read.input_tokens", u.Cached), attribute.Int("gen_ai.usage.cache_creation.input_tokens", u.CacheWrite))
	}
	if xcKey != "" && res.Response.FinishReason != "content_filter" {
		econ.PutExact(c, s.Redis, xcKey, econ.Cached{Content: res.Response.Content, ToolCalls: res.Response.ToolCalls, FinishReason: res.Response.FinishReason,
			Prompt: u.Prompt, Completion: u.Completion, Alias: res.Alias, Provider: res.Provider, Model: res.Model, CostE8: cost.E8(usd), At: start},
			time.Duration(max(1, s.Cfg.Econ.ExactCache.TTLSec))*time.Second)
	}

	// 6) Logs · Audit (GW-POL-04·05)
	ev.Decision, ev.CostUSD = "allow", costStr
	ev.Extra["route"], ev.Extra["alias_served"] = res.Provider+"/"+res.Model, res.Alias
	if !priced {
		ev.Extra["price"] = "unknown"
	}
	ev.Payload = s.payload(req.Messages, res.Response.Content)
	s.captureBodies(c, span, req.Messages, res.Response, vk)
	span.SetAttributes(attribute.String("gen_ai.provider.name", res.Provider), attribute.String("gen_ai.response.model", res.Model),
		attribute.Int("gen_ai.usage.input_tokens", u.Prompt), attribute.Int("gen_ai.usage.output_tokens", u.Completion),
		attribute.StringSlice("gen_ai.response.finish_reasons", []string{res.Response.FinishReason}),
		attribute.String("atlas.cost.usd", costStr), attribute.String("atlas.alias_served", res.Alias), attribute.Int("atlas.attempts", len(res.Attempts)))
	s.Audit.Record(ev)
	s.emitCompleted(c, reqID, vk, alias, res, &u, costStr, start, "ok", tags)

	usage := usageJSON(u)
	if req.Stream {
		s.stream(c, reqID, res, usage, generic)
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"id": reqID, "object": "chat.completion", "created": start.Unix(), "model": req.Model,
		"choices": []gin.H{{"index": 0, "message": assistantMessage(res.Response), "finish_reason": res.Response.FinishReason}},
		"usage":   usage,
	})
}

func usageJSON(u provider.Usage) gin.H {
	usage := gin.H{"prompt_tokens": u.Prompt, "completion_tokens": u.Completion, "total_tokens": u.Prompt + u.Completion}
	if u.Cached > 0 || u.CacheWrite > 0 {
		usage["prompt_tokens_details"] = gin.H{"cached_tokens": u.Cached, "cache_write_tokens": u.CacheWrite}
	}
	if u.Reasoning > 0 {
		usage["completion_tokens_details"] = gin.H{"reasoning_tokens": u.Reasoning}
	}
	return usage
}

func assistantMessage(r provider.Response) gin.H {
	m := gin.H{"role": "assistant", "content": r.Content}
	if len(r.ToolCalls) > 0 {
		m["tool_calls"] = r.ToolCalls
	}
	return m
}

// stream 은 SSE 로 응답을 청크 전송한다. M0: 업스트림 비스트림 응답을 청크로 중계(진짜 업스트림 스트림 중계는 M1).
// stream_options.include_usage 와 무관하게 마지막 청크에 usage 를 넣는다(FR-GW-ROUTE-08 usage 확정).
func (s *Server) stream(c *gin.Context, id string, res router.Result, usage gin.H, generic map[string]any) {
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	model, _ := generic["model"].(string)
	send := func(v any) {
		b, _ := json.Marshal(v)
		_, _ = fmt.Fprintf(c.Writer, "data: %s\n\n", b)
		c.Writer.Flush()
	}
	chunk := func(delta gin.H, finish any) gin.H {
		return gin.H{"id": id, "object": "chat.completion.chunk", "created": s.Now().Unix(), "model": model,
			"choices": []gin.H{{"index": 0, "delta": delta, "finish_reason": finish}}}
	}
	send(chunk(gin.H{"role": "assistant"}, nil))
	if len(res.Response.ToolCalls) > 0 { // 도구 호출은 한 청크로
		send(chunk(gin.H{"tool_calls": res.Response.ToolCalls}, nil))
	}
	runes := []rune(res.Response.Content)
	for i := 0; i < len(runes); i += 16 {
		send(chunk(gin.H{"content": string(runes[i:min(len(runes), i+16)])}, nil))
	}
	send(chunk(gin.H{}, res.Response.FinishReason))
	send(gin.H{"id": id, "object": "chat.completion.chunk", "created": s.Now().Unix(), "model": model, "choices": []gin.H{}, "usage": usage})
	_, _ = fmt.Fprint(c.Writer, "data: [DONE]\n\n")
	c.Writer.Flush()
}

func (s *Server) payload(msgs []provider.Message, out string) map[string]any {
	switch s.Cfg.Policy.PayloadLogging {
	case "full":
		return map[string]any{"messages": msgs, "output": out}
	case "masked":
		return map[string]any{"messages": fmt.Sprintf("[masked %d messages]", len(msgs)), "output": fmt.Sprintf("[masked len=%d]", len([]rune(out)))}
	default:
		return nil
	}
}

func (s *Server) accumulate(ctx context.Context, vk auth.VirtualKey, alias string, u provider.Usage, usd *big.Rat) {
	key := "gw:usage:" + vk.Prefix
	pipe := s.Redis.TxPipeline()
	pipe.HIncrBy(ctx, key, "requests", 1)
	pipe.HIncrBy(ctx, key, "prompt_tokens", int64(u.Prompt))
	pipe.HIncrBy(ctx, key, "completion_tokens", int64(u.Completion))
	pipe.HIncrBy(ctx, key, "cost_e8", cost.E8(usd))
	pipe.HIncrBy(ctx, key, "alias:"+alias, 1)
	if _, err := pipe.Exec(ctx); err != nil {
		slog.Warn("usage accumulate failed", "err", err)
	}
}

func (s *Server) emitCompleted(c *gin.Context, id string, vk auth.VirtualKey, alias string, res router.Result, u *provider.Usage,
	costUSD string, start time.Time, status string, tags map[string]string) {
	data := gin.H{"request_id": id, "tenant": vk.Tenant, "project": vk.Project, "key_prefix": vk.Prefix, "alias": alias,
		"alias_served": res.Alias, "provider": res.Provider, "model": res.Model, "status": status,
		"latency_ms": s.Now().Sub(start).Milliseconds(), "attempts": res.Attempts, "tags": tags, "cost_usd": costUSD}
	if u != nil {
		data["input"], data["output"], data["estimated"] = u.Prompt, u.Completion, u.Estimated
		data["cached_read"], data["cache_write"], data["reasoning"] = u.Cached, u.CacheWrite, u.Reasoning
	}
	if v := c.Writer.Header().Get("x-atlas-cache"); v != "" {
		data["exact_cache"] = v
	}
	go func() { // 응답 경로를 막지 않는다
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		if err := s.Events.Publish(ctx, "gw.request.completed", data); err != nil {
			slog.Warn("event publish failed", "err", err)
		}
	}()
}

func (s *Server) models(c *gin.Context) {
	vk, ok := s.authenticate(c)
	if !ok {
		return
	}
	data := []gin.H{}
	for _, a := range s.Cfg.AliasNames() {
		if vk.Allows(a) {
			data = append(data, gin.H{"id": a, "object": "model", "owned_by": "atlas-gateway"})
		}
	}
	c.JSON(http.StatusOK, gin.H{"object": "list", "data": data})
}

// captureBodies 는 정책에 따라 입력·출력 본문을 span 속성(gen_ai.input.messages·gen_ai.output.messages)에 남긴다.
// 비밀은 항상 제거하고 발견 시 gw.secret_leak_detected 이벤트를 낸다 (FR-OB-LOG-01·03).
func (s *Server) captureBodies(c *gin.Context, span trace.Span, msgs []provider.Message, r provider.Response, vk auth.VirtualKey) {
	mode := s.Capture
	span.SetAttributes(attribute.String("atlas.ob.capture", mode))
	in, n1, ok := capture.Body(mode, msgs)
	if !ok {
		return
	}
	out, n2, _ := capture.Body(mode, []any{assistantMessage(r)})
	span.SetAttributes(attribute.String("gen_ai.input.messages", in), attribute.String("gen_ai.output.messages", out))
	if n := n1 + n2; n > 0 {
		span.AddEvent("atlas.ob.secret_leak_detected", trace.WithAttributes(attribute.Int("count", n)))
		data := gin.H{"tenant": vk.Tenant, "key_prefix": vk.Prefix, "count": n, "request_id": c.GetHeader("x-atlas-request-id")}
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			_ = s.Events.Publish(ctx, "gw.secret_leak_detected", data)
		}()
	}
}
