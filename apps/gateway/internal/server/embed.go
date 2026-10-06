package server

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"

	"github.com/gin-gonic/gin"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"

	"atlas-gateway/internal/cost"
	"atlas-gateway/internal/provider"
)

// embeddings 는 OpenAI 호환 POST /v1/embeddings 다 (X-KB-02 — 임베딩도 게이트웨이 alias·키·비용 귀속을 거친다).
func (s *Server) embeddings(c *gin.Context) {
	start := s.Now()
	ctx := otel.GetTextMapPropagator().Extract(c.Request.Context(), propagation.HeaderCarrier(c.Request.Header))
	ctx, span := tracer.Start(ctx, "embeddings", trace.WithSpanKind(trace.SpanKindServer), trace.WithAttributes(attribute.String("gen_ai.operation.name", "embeddings")))
	defer span.End()
	vk, ok := s.authenticate(c)
	if !ok {
		return
	}
	raw, _ := io.ReadAll(io.LimitReader(c.Request.Body, 8<<20))
	var req struct {
		Model      string          `json:"model"`
		Input      json.RawMessage `json:"input"`
		Dimensions int             `json:"dimensions"`
	}
	if json.Unmarshal(raw, &req) != nil || req.Model == "" || len(req.Input) == 0 {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "missing_field", "model·input 필수")
		return
	}
	var inputs []string
	if json.Unmarshal(req.Input, &inputs) != nil {
		var one string
		if json.Unmarshal(req.Input, &one) != nil {
			oaiError(c, http.StatusBadRequest, "invalid_request_error", "bad_input", "input = 문자열 또는 문자열 배열")
			return
		}
		inputs = []string{one}
	}
	if len(inputs) > 2048 {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "too_many_inputs", "input 최대 2048개")
		return
	}
	if !s.Routes.Known(req.Model) {
		oaiError(c, http.StatusNotFound, "invalid_request_error", "model_not_found", "알 수 없는 alias: "+req.Model)
		return
	}
	if !vk.Allows(req.Model) {
		oaiError(c, http.StatusForbidden, "permission_error", "model_not_allowed", "이 키는 "+req.Model+" 를 호출할 수 없음")
		return
	}
	p, d, ok := s.Routes.Resolve(req.Model)
	emb, isEmb := p.(provider.Embedder)
	if !ok || !isEmb {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "not_embedding_model", "임베딩을 지원하지 않는 alias")
		return
	}
	dims := req.Dimensions
	if dims == 0 {
		dims = 256
	}
	vecs, tokens, err := emb.Embed(ctx, d.Model, inputs, dims)
	if err != nil {
		var pe *provider.Error
		if errors.As(err, &pe) && pe.Status >= 400 && pe.Status < 500 {
			oaiError(c, http.StatusBadRequest, "invalid_request_error", "not_embedding_model", err.Error())
			return
		}
		oaiError(c, http.StatusBadGateway, "api_error", "upstream_error", err.Error())
		return
	}
	tags := parseTags(c.GetHeader("x-atlas-tags"), nil)
	usd := s.Prices.CostCached(d.Model, tokens, 0, 0, 0, start).USD
	u := provider.Usage{Prompt: tokens}
	s.accumulate(c, vk, req.Model, u, usd)
	c.Header("x-atlas-cost-usd", cost.Format(usd))
	c.Header("x-atlas-route", d.Provider+"/"+d.Model)
	span.SetAttributes(attribute.String("atlas.tenant", vk.Tenant), attribute.String("gen_ai.request.model", req.Model), attribute.String("gen_ai.response.model", d.Model),
		attribute.Int("gen_ai.usage.input_tokens", tokens), attribute.String("atlas.cost.usd", cost.Format(usd)), attribute.String("atlas.unit_id", tags["unit_id"]))
	data := make([]gin.H, len(vecs))
	for i, v := range vecs {
		data[i] = gin.H{"object": "embedding", "index": i, "embedding": v}
	}
	c.JSON(http.StatusOK, gin.H{"object": "list", "model": req.Model, "data": data, "usage": gin.H{"prompt_tokens": tokens, "total_tokens": tokens}})
}
