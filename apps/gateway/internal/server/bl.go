package server

import (
	"errors"
	"net/http"
	"sort"
	"strconv"

	"github.com/gin-gonic/gin"

	"atlas-gateway/internal/aliases"
	"atlas-gateway/internal/config"
	"atlas-gateway/internal/econ"
)

// alias 관리 (FR-BL-MODEL-02·05·06): 버전 기록·롤백·서빙 통계 — BL 서비스(모델 수명 주기)가 부른다.

func (s *Server) needAliases(c *gin.Context) bool {
	if s.Aliases == nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "aliases_disabled", "alias 오버레이 비활성")
		return false
	}
	return true
}

func (s *Server) listAliases(c *gin.Context) {
	if !s.needAliases(c) {
		return
	}
	names := map[string]bool{}
	for n := range s.Cfg.Aliases {
		names[n] = true
	}
	ov, _ := s.Aliases.All(c)
	for n := range ov {
		names[n] = true
	}
	out := make([]aliases.Entry, 0, len(names))
	for n := range names {
		e, _ := s.Aliases.Current(c, n)
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	c.JSON(http.StatusOK, gin.H{"data": out})
}

func (s *Server) getAlias(c *gin.Context) {
	if !s.needAliases(c) {
		return
	}
	e, ok := s.Aliases.Current(c, c.Param("name"))
	if !ok {
		oaiError(c, http.StatusNotFound, "invalid_request_error", "alias_not_found", "alias 없음")
		return
	}
	c.JSON(http.StatusOK, e)
}

func (s *Server) putAlias(c *gin.Context) {
	if !s.needAliases(c) {
		return
	}
	var e aliases.Entry
	if err := c.ShouldBindJSON(&e); err != nil {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "bad_body", err.Error())
		return
	}
	e.Name = c.Param("name")
	if err := s.Aliases.Validate(&e, s.Routes.Known); err != nil {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "invalid_alias", err.Error())
		return
	}
	out, err := s.Aliases.Put(c, e)
	if err != nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "alias_backend", err.Error())
		return
	}
	s.reloadAliases(c)
	if s.Events != nil {
		_ = s.Events.Publish(c, "gw.alias.changed", gin.H{"alias": out.Name, "version": out.Version, "deployments": out.Deployments,
			"fallbacks": out.Fallbacks, "note": out.Note, "by": out.By})
	}
	c.JSON(http.StatusOK, out)
}

func (s *Server) reloadAliases(c *gin.Context) {
	all, err := s.Aliases.All(c)
	if err != nil {
		return
	}
	ov := map[string]config.Alias{}
	for k, e := range all {
		ov[k] = e.Alias()
	}
	s.Routes.SetOverlay(ov)
}

func (s *Server) deleteAlias(c *gin.Context) {
	if !s.needAliases(c) {
		return
	}
	if err := s.Aliases.Delete(c, c.Param("name")); err != nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "alias_backend", err.Error())
		return
	}
	s.reloadAliases(c)
	c.Status(http.StatusNoContent)
}

func (s *Server) aliasHistory(c *gin.Context) {
	if !s.needAliases(c) {
		return
	}
	c.JSON(http.StatusOK, gin.H{"data": s.Aliases.History(c, c.Param("name"))})
}

func (s *Server) rollbackAlias(c *gin.Context) {
	if !s.needAliases(c) {
		return
	}
	var r struct {
		Version int    `json:"version"`
		By      string `json:"by"`
	}
	_ = c.ShouldBindJSON(&r)
	out, err := s.Aliases.Rollback(c, c.Param("name"), r.Version, r.By)
	if err != nil {
		code := http.StatusServiceUnavailable
		if errors.Is(err, aliases.ErrInvalid) {
			code = http.StatusBadRequest
		}
		oaiError(c, code, "invalid_request_error", "rollback_failed", err.Error())
		return
	}
	s.reloadAliases(c)
	if s.Events != nil {
		_ = s.Events.Publish(c, "gw.alias.changed", gin.H{"alias": out.Name, "version": out.Version, "note": out.Note, "by": out.By})
	}
	c.JSON(http.StatusOK, out)
}

func (s *Server) aliasStats(c *gin.Context) {
	if !s.needAliases(c) {
		return
	}
	e, ok := s.Aliases.Current(c, c.Param("name"))
	if !ok {
		oaiError(c, http.StatusNotFound, "invalid_request_error", "alias_not_found", "alias 없음")
		return
	}
	v := e.Version
	if q := c.Query("version"); q != "" {
		v, _ = strconv.Atoi(q)
	}
	var models []string
	for _, d := range e.Deployments {
		models = append(models, d.Model)
	}
	c.JSON(http.StatusOK, gin.H{"alias": e.Name, "version": v, "models": s.Aliases.Stats(c, e.Name, v, models)})
}

// 계산 정책 (FR-BL-COMP-01·05)

func (s *Server) needCompute(c *gin.Context) bool {
	if s.Compute == nil {
		oaiError(c, http.StatusServiceUnavailable, "api_error", "compute_disabled", "계산 정책 비활성")
		return false
	}
	return true
}

func (s *Server) listCompute(c *gin.Context) {
	if s.needCompute(c) {
		c.JSON(http.StatusOK, gin.H{"data": s.Compute.List(c)})
	}
}

func (s *Server) putCompute(c *gin.Context) {
	if !s.needCompute(c) {
		return
	}
	var p econ.ComputePolicy
	if err := c.ShouldBindJSON(&p); err != nil {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "bad_body", err.Error())
		return
	}
	p.ID = c.Param("id")
	out, err := s.Compute.Put(c, p)
	if err != nil {
		oaiError(c, http.StatusBadRequest, "invalid_request_error", "invalid_policy", err.Error())
		return
	}
	c.JSON(http.StatusOK, out)
}

func (s *Server) deleteCompute(c *gin.Context) {
	if s.needCompute(c) {
		_ = s.Compute.Delete(c, c.Param("id"))
		c.Status(http.StatusNoContent)
	}
}

func (s *Server) computeUsage(c *gin.Context) {
	if !s.needCompute(c) {
		return
	}
	u := s.Compute.Usage(c, c.Param("id"))
	if u == nil {
		oaiError(c, http.StatusNotFound, "invalid_request_error", "policy_not_found", "정책 없음")
		return
	}
	c.JSON(http.StatusOK, u)
}
