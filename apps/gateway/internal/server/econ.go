package server

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"math/big"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"atlas-gateway/internal/audit"
	"atlas-gateway/internal/auth"
	"atlas-gateway/internal/cost"
	"atlas-gateway/internal/econ"
	"atlas-gateway/internal/provider"
	"atlas-gateway/internal/router"
)

// serveExact 는 exact 캐시 적중 응답이다: 모델 호출 없음, 비용 0, x-atlas-cache: HIT-EXACT (FR-EC-CACHE-04).
func (s *Server) serveExact(c *gin.Context, reqID string, vk auth.VirtualKey, req chatRequest, alias string, hit *econ.Cached,
	ev audit.Event, start time.Time, tags map[string]string, generic map[string]any, span trace.Span) {
	c.Header("x-atlas-cache", "HIT-EXACT")
	c.Header("x-atlas-cost-usd", "0.00000000")
	c.Header("x-atlas-route", hit.Provider+"/"+hit.Model)
	c.Header("x-atlas-cache-age", (time.Duration(s.Now().Sub(hit.At).Seconds()) * time.Second).String())
	econ.RecordExactHit(c, s.Redis, vk.Tenant, hit.Alias, hit.CostE8)
	span.SetAttributes(attribute.String("atlas.cache", "hit-exact"), attribute.String("atlas.cost.usd", "0.00000000"))
	ev.Decision, ev.CostUSD = "allow", "0.00000000"
	ev.Extra["cache"], ev.Extra["alias_served"] = "hit-exact", hit.Alias
	s.Audit.Record(ev)
	res := router.Result{Alias: hit.Alias, Provider: hit.Provider, Model: hit.Model,
		Response: provider.Response{Content: hit.Content, ToolCalls: hit.ToolCalls, FinishReason: hit.FinishReason}}
	s.captureBodies(c, span, req.Messages, res.Response, vk) // 캐시 적중도 trace 에 본문 기록 정책 적용 (OB-LOG-01 · 골든셋 승격)
	zero := provider.Usage{}
	s.emitCompleted(c, reqID, vk, alias, res, &zero, "0.00000000", start, "ok", tags)
	usage := gin.H{"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_response": true}
	if req.Stream {
		s.stream(c, reqID, res, usage, generic)
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"id": reqID, "object": "chat.completion", "created": start.Unix(), "model": req.Model,
		"choices": []gin.H{{"index": 0, "message": assistantMessage(res.Response), "finish_reason": hit.FinishReason}},
		"usage":   usage,
	})
}

func (s *Server) catalog(c *gin.Context) {
	now := s.Now()
	c.JSON(http.StatusOK, gin.H{"models": econ.Catalog(s.Cfg, now, func(m string) map[string]string {
		in, out, cr, cw, ok := s.Prices.Prices(m, now)
		if !ok {
			return nil
		}
		return map[string]string{"input": in.FloatString(4), "output": out.FloatString(4), "cached_input": cr.FloatString(4), "cache_write": cw.FloatString(4)}
	}), "task_policies": s.Cfg.Econ.ModelPolicy, "routes": s.Cfg.Econ.Routes})
}

func (s *Server) cacheKPI(c *gin.Context) {
	k, err := econ.KPI(c, s.Redis, c.Query("tenant"))
	if err != nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "backend_unavailable", err.Error())
		return
	}
	c.JSON(http.StatusOK, gin.H{"data": k})
}

func (s *Server) taskUsage(c *gin.Context) {
	if s.Tasks == nil {
		oaiError(c, http.StatusNotFound, "invalid_request_error", "disabled", "과업 예산 비활성")
		return
	}
	c.JSON(http.StatusOK, gin.H{"task_id": c.Param("id"), "used_tokens": s.Tasks.Used(c, c.Param("id")), "limit": s.Tasks.Default})
}

func (s *Server) listBudgets(c *gin.Context) {
	if s.Budgets == nil {
		c.JSON(http.StatusOK, gin.H{"data": []any{}})
		return
	}
	l, err := s.Budgets.List(c)
	if err != nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "backend_unavailable", err.Error())
		return
	}
	c.JSON(http.StatusOK, gin.H{"data": l})
}

func (s *Server) putBudget(c *gin.Context) {
	if s.Budgets == nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "disabled", "예산 비활성")
		return
	}
	var b econ.Budget
	if err := c.ShouldBindJSON(&b); err != nil {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "invalid_json", err.Error())
		return
	}
	b.ID = c.Param("id")
	if err := s.Budgets.Validate(&b, func(a string) bool { _, ok := s.Cfg.Aliases[a]; return ok }); err != nil {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "invalid_budget", err.Error())
		return
	}
	if err := s.Budgets.Put(c, b); err != nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "backend_unavailable", err.Error())
		return
	}
	s.Audit.Record(audit.Event{TS: s.Now().UTC(), Actor: "admin", Action: "budget.put", Resource: b.ID, Decision: "allow",
		Extra: map[string]string{"scope": b.Scope + ":" + b.ScopeID, "amount_usd": b.AmountUSD, "hard_action": b.HardAction}})
	c.JSON(http.StatusOK, b)
}

func (s *Server) deleteBudget(c *gin.Context) {
	if s.Budgets == nil {
		c.Status(http.StatusNotFound)
		return
	}
	ok, err := s.Budgets.Delete(c, c.Param("id"))
	switch {
	case err != nil:
		oaiError(c, http.StatusServiceUnavailable, "api_error", "backend_unavailable", err.Error())
	case !ok:
		oaiError(c, http.StatusNotFound, "invalid_request_error", "not_found", "예산 없음")
	default:
		s.Audit.Record(audit.Event{TS: s.Now().UTC(), Actor: "admin", Action: "budget.delete", Resource: c.Param("id"), Decision: "allow"})
		c.Status(http.StatusNoContent)
	}
}

// simulate 는 순수 계산(데이터 접근 없음)이라 인증 없이 연다 — 콘솔 폼에서 사용 (FR-EC-CTRL-06).
func (s *Server) simulate(c *gin.Context) {
	var in econ.SimInput
	if err := c.ShouldBindJSON(&in); err != nil {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "invalid_json", err.Error())
		return
	}
	disc, _ := strconv.ParseFloat(s.Cfg.Econ.BatchDiscount, 64)
	now := s.Now()
	out, err := econ.Simulate(in, econ.RatPrices(s.Prices.Prices, now), disc, now)
	if err != nil {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "invalid_simulation", err.Error())
		return
	}
	c.JSON(http.StatusOK, out)
}

// ---- 배치 (FR-EC-BATCH-01·02) ----

// BatchCost 는 배치 회차 비용을 가상키 사용량·예산에 정산한다 (econ.Batches.OnCost).
func (s *Server) BatchCost(ctx context.Context, b econ.Batch, usd *big.Rat, prompt, completion int) {
	s.accumulate(ctx, auth.VirtualKey{Prefix: b.KeyPrefix, Tenant: b.Tenant}, b.Alias+":batch", provider.Usage{Prompt: prompt, Completion: completion}, usd)
	if s.Budgets != nil {
		s.Budgets.Settle(ctx, econ.Scopes{Org: b.Tenant, Project: b.Project, Key: b.KeyPrefix}, cost.E8(usd))
	}
}

func (s *Server) batchesReady(c *gin.Context) (auth.VirtualKey, bool) {
	vk, ok := s.authenticate(c)
	if !ok {
		return vk, false
	}
	if s.Batches == nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "batches_disabled", "배치 비활성")
		return vk, false
	}
	return vk, true
}

func (s *Server) createBatch(c *gin.Context) {
	vk, ok := s.batchesReady(c)
	if !ok {
		return
	}
	raw, err := io.ReadAll(io.LimitReader(c.Request.Body, 64<<20))
	if err != nil {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "bad_body", err.Error())
		return
	}
	jsonl, meta := raw, map[string]string(nil)
	if ct := c.ContentType(); ct == "application/json" {
		var body struct {
			Input            string            `json:"input"`
			Requests         []json.RawMessage `json:"requests"`
			CompletionWindow string            `json:"completion_window"`
			Metadata         map[string]string `json:"metadata"`
		}
		if err := json.Unmarshal(raw, &body); err != nil {
			oaiError(c, http.StatusBadRequest, "invalid_request_error", "invalid_json", err.Error())
			return
		}
		if body.CompletionWindow != "" && body.CompletionWindow != "24h" {
			oaiError(c, http.StatusBadRequest, "invalid_request_error", "invalid_window", "completion_window 는 24h 만")
			return
		}
		meta, jsonl = body.Metadata, []byte(body.Input)
		if len(body.Requests) > 0 {
			parts := make([]string, len(body.Requests))
			for i, r := range body.Requests {
				parts[i] = string(r)
			}
			jsonl = []byte(strings.Join(parts, "\n"))
		}
	}
	b, err := s.Batches.Create(c, econ.Owner{Tenant: vk.Tenant, Project: vk.Project, KeyPrefix: vk.Prefix}, jsonl, vk.Allows, meta)
	switch {
	case errors.Is(err, econ.ErrBatchInput):
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "invalid_batch", err.Error())
	case err != nil:
		oaiError(c, http.StatusBadGateway, "api_error", "batch_submit_failed", err.Error())
	default:
		s.Audit.Record(audit.Event{TS: s.Now().UTC(), Actor: vk.Prefix, Tenant: vk.Tenant, Action: "batch.create", Resource: b.ID, Decision: "allow",
			Extra: map[string]string{"alias": b.Alias, "rows": strconv.Itoa(b.Total)}})
		c.JSON(http.StatusOK, b)
	}
}

func (s *Server) listBatches(c *gin.Context) {
	vk, ok := s.batchesReady(c)
	if !ok {
		return
	}
	l, err := s.Batches.List(c, vk.Tenant, 50)
	if err != nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "backend_unavailable", err.Error())
		return
	}
	c.JSON(http.StatusOK, gin.H{"object": "list", "data": l})
}

func (s *Server) batchErr(c *gin.Context, err error) {
	switch {
	case errors.Is(err, econ.ErrBatchNotFound):
		oaiError(c, http.StatusNotFound, "invalid_request_error", "batch_not_found", "배치 없음")
	case errors.Is(err, econ.ErrBatchState):
		oaiError(c, http.StatusConflict, "invalid_request_error", "batch_state", err.Error())
	default:
		oaiError(c, http.StatusBadGateway, "api_error", "batch_error", err.Error())
	}
}

func (s *Server) getBatch(c *gin.Context) {
	vk, ok := s.batchesReady(c)
	if !ok {
		return
	}
	b, err := s.Batches.Get(c, vk.Tenant, c.Param("id"))
	if err != nil {
		s.batchErr(c, err)
		return
	}
	c.JSON(http.StatusOK, b)
}

func (s *Server) batchOutput(c *gin.Context) {
	vk, ok := s.batchesReady(c)
	if !ok {
		return
	}
	out, err := s.Batches.Output(c, vk.Tenant, c.Param("id"))
	if err != nil {
		s.batchErr(c, err)
		return
	}
	c.Data(http.StatusOK, "application/jsonl", out)
}

func (s *Server) retryBatch(c *gin.Context) {
	vk, ok := s.batchesReady(c)
	if !ok {
		return
	}
	b, err := s.Batches.Retry(c, vk.Tenant, c.Param("id"))
	if err != nil {
		s.batchErr(c, err)
		return
	}
	c.JSON(http.StatusOK, b)
}

// purgeExact 는 exact 응답 캐시를 비운다 (FR-X-KB-08 삭제권 전파 — 캐시 키에 주체 색인이 없으므로 전체 삭제).
func (s *Server) purgeExact(c *gin.Context) {
	n, err := econ.PurgeExact(c, s.Redis)
	if err != nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "backend_unavailable", err.Error())
		return
	}
	c.JSON(http.StatusOK, gin.H{"store": "exact_cache", "deleted": n})
}
