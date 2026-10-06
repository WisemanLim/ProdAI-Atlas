package server

import (
	"atlas-gateway/internal/audit"
	"atlas-gateway/internal/keyrot"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"atlas-gateway/internal/auth"
)

type issueRequest struct {
	Tenant           string            `json:"tenant"`
	Project          string            `json:"project"`
	Models           []string          `json:"models"`
	RPM              int               `json:"rpm"`
	TPM              int               `json:"tpm"`
	ExpiresInSeconds int               `json:"expires_in_seconds"`
	Metadata         map[string]string `json:"metadata"`
}

func (s *Server) issueKey(c *gin.Context) {
	var r issueRequest
	if err := c.ShouldBindJSON(&r); err != nil || r.Tenant == "" {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "invalid_body", "tenant 필수")
		return
	}
	if len(r.Models) == 0 {
		r.Models = []string{"*"}
	}
	if r.Project == "" {
		r.Project = "default"
	}
	vk := auth.VirtualKey{Tenant: r.Tenant, Project: r.Project, Models: r.Models, RPM: r.RPM, TPM: r.TPM, Metadata: r.Metadata}
	if r.ExpiresInSeconds > 0 {
		t := s.Now().UTC().Add(time.Duration(r.ExpiresInSeconds) * time.Second)
		vk.ExpiresAt = &t
	}
	plain, _, err := s.Keys.Issue(c, vk)
	if err != nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "auth_backend_unavailable", err.Error())
		return
	}
	got, _ := s.Keys.Lookup(c, plain)
	c.JSON(http.StatusCreated, gin.H{"key": plain, "virtualKey": got, "note": "평문 키는 지금 한 번만 표시됩니다"})
}

func (s *Server) listKeys(c *gin.Context) {
	keys, err := s.Keys.List(c)
	if err != nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "auth_backend_unavailable", err.Error())
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": keys})
}

func (s *Server) revokeKey(c *gin.Context) {
	ok, err := s.Keys.Revoke(c, c.Param("prefix"))
	switch {
	case err != nil:
		oaiError(c, http.StatusServiceUnavailable, "api_error", "auth_backend_unavailable", err.Error())
	case !ok:
		oaiError(c, http.StatusNotFound, "invalid_request_error", "not_found", "키 없음")
	default:
		c.Status(http.StatusNoContent)
	}
}

// usage 는 키별 누적 사용량이다(GET /admin/usage?key_prefix=...). 기간·그룹 집계는 ClickHouse 도입(M1) 후.
func (s *Server) usage(c *gin.Context) {
	prefix := c.Query("key_prefix")
	if prefix == "" {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "missing_key_prefix", "key_prefix 필수")
		return
	}
	h, err := s.Redis.HGetAll(c, "gw:usage:"+prefix).Result()
	if err != nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "redis_unavailable", err.Error())
		return
	}
	out := gin.H{"key_prefix": prefix, "by_alias": gin.H{}}
	for k, v := range h {
		n, _ := strconv.ParseInt(v, 10, 64)
		switch {
		case k == "cost_e8":
			out["cost_usd"] = strconv.FormatFloat(float64(n)/1e8, 'f', 8, 64)
		case strings.HasPrefix(k, "alias:"):
			out["by_alias"].(gin.H)[strings.TrimPrefix(k, "alias:")] = n
		default:
			out[k] = n
		}
	}
	c.JSON(http.StatusOK, out)
}

func (s *Server) upstreamKeys(c *gin.Context) {
	out := gin.H{}
	for name, p := range s.Pools {
		out[name] = p.Status()
	}
	c.JSON(http.StatusOK, out)
}

func (s *Server) compromiseKey(c *gin.Context) {
	p, ok := s.Pools[c.Param("provider")]
	idx, err := strconv.Atoi(c.Param("index"))
	if !ok || err != nil {
		oaiError(c, http.StatusNotFound, "invalid_request_error", "not_found", "provider/index 없음")
		return
	}
	if err := p.Compromise(idx); err != nil {
		oaiError(c, http.StatusNotFound, "invalid_request_error", "not_found", err.Error())
		return
	}
	c.JSON(http.StatusOK, gin.H{"provider": c.Param("provider"), "keys": p.Status()})
}

// rotateKey 는 공급자 키 무중단 회전이다 (FR-PLT-SEC-04): 새 키 검증 호출 → current 교체 → 이전 키 24h retiring.
func (s *Server) rotateKey(c *gin.Context) {
	if s.KeyRot == nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "secret_store_disabled", "비밀 저장소 미설정 (ATLAS_SECRET_STORE)")
		return
	}
	var r struct {
		Key string `json:"key"`
	}
	if err := c.ShouldBindJSON(&r); err != nil || r.Key == "" {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "missing_key", "key 필수")
		return
	}
	st, err := s.KeyRot.Rotate(c, c.Param("provider"), r.Key)
	switch {
	case errors.Is(err, keyrot.ErrInvalidKey):
		oaiError(c, http.StatusUnprocessableEntity, "invalid_request_error", "key_validation_failed", "새 키 검증 실패 — 교체하지 않음")
	case err != nil:
		oaiError(c, http.StatusBadGateway, "api_error", "rotation_failed", err.Error())
	default:
		s.Audit.Record(audit.Event{TS: s.Now().UTC(), Actor: "admin", Action: "upstream_key.rotate", Resource: c.Param("provider"), Decision: "allow",
			Extra: map[string]string{"version": strconv.Itoa(st.Version)}})
		c.JSON(http.StatusOK, st)
	}
}

func (s *Server) rotationStatus(c *gin.Context) {
	if s.KeyRot == nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "secret_store_disabled", "비밀 저장소 미설정")
		return
	}
	st, err := s.KeyRot.Status(c, c.Param("provider"))
	if err != nil {
		oaiError(c, http.StatusNotFound, "invalid_request_error", "not_found", err.Error())
		return
	}
	c.JSON(http.StatusOK, st)
}
