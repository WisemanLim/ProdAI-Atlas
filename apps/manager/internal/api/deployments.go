package api

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/gorm"

	"atlas-manager/internal/store"
)

type deploymentView struct {
	store.Deployment
	AgentName string         `json:"agentName"`
	UnitID    string         `json:"unitId"`
	Card      map[string]any `json:"card,omitempty"`
}

func (s *Server) deploymentView(d store.Deployment, a store.Agent) deploymentView {
	v := deploymentView{Deployment: d, AgentName: a.Name, UnitID: a.UnitID}
	if d.Card != nil {
		_ = json.Unmarshal([]byte(*d.Card), &v.Card)
	}
	return v
}

type deployRequest struct {
	AgentID  string `json:"agentId"`
	Env      string `json:"env"`
	Driver   string `json:"driver"`
	Endpoint string `json:"endpoint"`
}

var drivers = map[string]bool{"local": true, "compose": true, "kubernetes": true}

// createDeployment 는 에이전트 현재 리비전을 환경에 배포 등록한다. 같은 (agent, env) 의 활성 배포가 있으면 갱신한다(멱등).
// M0: 실제 기동은 compose/로컬 프로세스가 하고, Manager 는 endpoint 를 폴링해 상태를 수집한다(FR-PLT-MGR-05).
func (s *Server) createDeployment(c *gin.Context) {
	var r deployRequest
	if err := c.ShouldBindJSON(&r); err != nil {
		fail(c, http.StatusBadRequest, "bad_request", err.Error(), nil)
		return
	}
	if r.Env == "" {
		r.Env = "dev"
	}
	if r.Driver == "" {
		r.Driver = "compose"
	}
	u, err := url.Parse(r.Endpoint)
	if !drivers[r.Driver] || err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		fail(c, http.StatusUnprocessableEntity, "invalid_deployment", "driver(local|compose|kubernetes)·endpoint(http[s]://host[:port]) 확인",
			gin.H{"driver": r.Driver, "endpoint": r.Endpoint})
		return
	}
	org := orgOf(c)
	s.tenantScope(c, org)
	db := s.db(c)
	var a store.Agent
	if err := db.First(&a, "id = ? AND org_id = ?", r.AgentID, org).Error; errors.Is(err, gorm.ErrRecordNotFound) {
		fail(c, http.StatusNotFound, "not_found", "에이전트 없음", nil)
		return
	} else if err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	if _, ok := s.orgGuard(c, org); !ok || !s.require(c, "deploy", envObj(org, a.Project, r.Env)) {
		return
	}
	var curRev store.Revision
	db.First(&curRev, "agent_id = ? AND rev = ?", a.ID, a.CurrentRev)
	if !s.enforcePolicy(c, org, r.Env, curRev, a) { // 배포 전 서명·정책 검사 (FR-PLT-DEP-10)
		return
	}
	now := time.Now().UTC()
	endpoint := strings.TrimRight(r.Endpoint, "/")
	active := func(d *store.Deployment) error {
		return db.Where("agent_id = ? AND env = ? AND slot = ? AND status <> ?", a.ID, r.Env, store.SlotPrimary, store.DeployStopped).First(d).Error
	}
	var d store.Deployment
	err = active(&d)
	code := http.StatusOK
	if errors.Is(err, gorm.ErrRecordNotFound) {
		d = store.Deployment{ID: uuid.NewString(), OrgID: org, AgentID: a.ID, Rev: a.CurrentRev, Env: r.Env, Driver: r.Driver, Endpoint: endpoint,
			Status: store.DeployPending, Slot: store.SlotPrimary, Weight: 100, Replicas: 1, CreatedAt: now, UpdatedAt: now}
		err, code = db.Create(&d).Error, http.StatusCreated
		if errors.Is(err, gorm.ErrDuplicatedKey) { // 동시 등록 경쟁에서 짐 → 이긴 쪽 배포를 갱신 경로로
			d, code = store.Deployment{}, http.StatusOK
			err = active(&d)
		}
	}
	if err == nil && code == http.StatusOK {
		changed := d.Rev != a.CurrentRev || d.Endpoint != endpoint || d.Driver != r.Driver
		d.Rev, d.Endpoint, d.Driver = a.CurrentRev, endpoint, r.Driver
		if changed {
			d.Status, d.FailCount, d.CreatedAt, d.UpdatedAt = store.DeployStarting, 0, now, now
		}
		err = db.Save(&d).Error
	}
	if err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	if code == http.StatusCreated {
		s.emit(c, "plt.deploy.started", gin.H{"id": d.ID, "org": org, "agentId": a.ID, "name": a.Name, "env": d.Env, "rev": d.Rev, "driver": d.Driver})
		s.audit(c, org, "deploy.create", "deployment:"+d.ID, "success", nil, gin.H{"agent": a.Name, "env": d.Env, "rev": d.Rev}, nil)
	}
	c.JSON(code, s.deploymentView(d, a))
}

func (s *Server) listDeployments(c *gin.Context) {
	org := orgOf(c)
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	db := s.db(c)
	q := db.Where("org_id = ?", org).Order("created_at DESC")
	if id := c.Query("agentId"); id != "" {
		q = q.Where("agent_id = ?", id)
	}
	if st := c.Query("status"); st != "" {
		q = q.Where("status = ?", st)
	}
	var deps []store.Deployment
	if err := q.Find(&deps).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	items := make([]deploymentView, 0, len(deps))
	for _, d := range deps {
		var a store.Agent
		if db.Unscoped().First(&a, "id = ?", d.AgentID).Error == nil {
			items = append(items, s.deploymentView(d, a))
		}
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func (s *Server) findDeployment(c *gin.Context, relation string) (store.Deployment, store.Agent, bool) {
	var d store.Deployment
	var a store.Agent
	org := orgOf(c)
	s.tenantScope(c, org)
	db := s.db(c)
	if err := db.First(&d, "id = ? AND org_id = ?", c.Param("id"), org).Error; err != nil {
		fail(c, http.StatusNotFound, "not_found", "배포 없음", nil)
		return d, a, false
	}
	db.Unscoped().First(&a, "id = ?", d.AgentID)
	obj := projObj(org, a.Project)
	if relation == "deploy" {
		obj = envObj(org, a.Project, d.Env)
	}
	if !s.require(c, relation, obj) {
		return d, a, false
	}
	return d, a, true
}

func (s *Server) getDeployment(c *gin.Context) {
	if d, a, ok := s.findDeployment(c, "viewer"); ok {
		c.JSON(http.StatusOK, s.deploymentView(d, a))
	}
}

func (s *Server) stopDeployment(c *gin.Context) {
	d, a, ok := s.findDeployment(c, "deploy")
	if !ok {
		return
	}
	if err := s.stop(c, []store.Deployment{d}); err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	d.Status = store.DeployStopped
	s.audit(c, d.OrgID, "deploy.stop", "deployment:"+d.ID, "success", nil, gin.H{"agent": a.Name, "env": d.Env}, nil)
	c.JSON(http.StatusOK, s.deploymentView(d, a))
}

// stop 은 배포를 stopped 로 바꾸고 라우팅에서 뺀다 (agent-proxy 는 ready 만 라우팅).
func (s *Server) stop(c *gin.Context, deps []store.Deployment) error {
	for _, d := range deps {
		if d.Status == store.DeployStopped {
			continue
		}
		if err := s.db(c).Model(&store.Deployment{}).Where("id = ?", d.ID).
			Updates(map[string]any{"status": store.DeployStopped, "updated_at": time.Now().UTC()}).Error; err != nil {
			return err
		}
		s.emit(c, "plt.deploy.progress", gin.H{"id": d.ID, "agentId": d.AgentID, "env": d.Env, "from": d.Status, "to": store.DeployStopped})
	}
	return nil
}

// proxyA2A 는 agent-proxy 다 (FR-PLT-MGR-10): ready 배포의 Agent Card A2A URL 로 JSON-RPC 를 중계하고 traceparent 를 전달한다.
// 인증 모드에서는 호출자 신원·조직을 담은 단기 JWT(ES256, 5분)를 붙이고 params.tenant 를 조직으로 강제한다 (FR-PLT-TEN-06).
func (s *Server) proxyA2A(c *gin.Context) {
	org := orgOf(c)
	var o store.Organization
	if s.DB.WithContext(c).First(&o, "slug = ?", org).Error == nil && o.Status != "active" {
		fail(c, http.StatusUnauthorized, "org_"+o.Status, "조직이 "+o.Status+" 상태라 에이전트 호출 불가", nil) // FR-PLT-TEN-01
		return
	}
	a, ok := s.findAgent(c, "invoke")
	if !ok {
		return
	}
	release, ok := s.admitRun(c, org) // 워크스페이스 할당량: 월 토큰·동시 실행 (FR-X-WSP-08)
	if !ok {
		return
	}
	defer release()
	env := c.DefaultQuery("env", "dev")
	caller := firstNonEmpty(principal(c).Sub, "anonymous")
	d, err := pickDeployment(s.db(c), a.ID, env, firstNonEmpty(c.GetHeader("x-atlas-assign-key"), caller)) // canary 가중 라우팅 (FR-PLT-DEP-05)
	s.finishTx(c)                                                                                          // 중계(최대 60초) 동안 DB 트랜잭션을 잡지 않는다
	if err != nil {
		fail(c, http.StatusServiceUnavailable, "no_ready_deployment", "ready 상태 배포 없음", gin.H{"agent": a.Name})
		return
	}
	target := d.Endpoint + "/a2a"
	if d.Card != nil {
		var card struct {
			SupportedInterfaces []struct {
				URL             string `json:"url"`
				ProtocolBinding string `json:"protocolBinding"`
			} `json:"supportedInterfaces"`
		}
		if json.Unmarshal([]byte(*d.Card), &card) == nil {
			for _, it := range card.SupportedInterfaces {
				if it.ProtocolBinding == "JSONRPC" && it.URL != "" {
					target = it.URL
					break
				}
			}
		}
	}
	body, err := io.ReadAll(io.LimitReader(c.Request.Body, 4<<20))
	if err != nil {
		fail(c, http.StatusBadRequest, "bad_request", err.Error(), nil)
		return
	}
	if body, ok = s.enforceTenant(c, body, org, "agent:"+a.ID); !ok {
		return
	}
	req, _ := http.NewRequestWithContext(c, http.MethodPost, target, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if s.Signer != nil {
		p := principal(c)
		if tok, err := s.Signer.Mint(firstNonEmpty(p.Sub, "anonymous"), "agent:"+a.ID, org, a.Project, env, 5*time.Minute); err == nil {
			req.Header.Set("Authorization", "Bearer "+tok)
		}
	}
	req.Header.Set("baggage", mergeBaggage(c.GetHeader("baggage"), "atlas.tenant="+org))
	for _, h := range []string{"traceparent", "tracestate"} {
		if v := c.GetHeader(h); v != "" {
			req.Header.Set(h, v)
		}
	}
	client := s.Proxy
	if streamingMethod(body) { // SSE 는 응답이 길다 — 전체 타임아웃 대신 호출자 연결 수명(요청 컨텍스트)을 따른다
		client = &http.Client{Transport: s.Proxy.Transport}
	}
	res, err := client.Do(req)
	if err != nil {
		s.recordCall(c, d, caller, true)
		fail(c, http.StatusBadGateway, "agent_unreachable", err.Error(), gin.H{"target": target})
		return
	}
	defer res.Body.Close()
	s.recordCall(c, d, caller, res.StatusCode >= 500)
	c.Header("x-atlas-deployment", d.ID)
	c.Header("x-atlas-deployment-slot", d.Slot)
	if strings.HasPrefix(res.Header.Get("Content-Type"), "text/event-stream") {
		c.Header("Content-Type", "text/event-stream")
		c.Header("Cache-Control", "no-cache")
		c.Status(res.StatusCode)
		buf := make([]byte, 16<<10)
		for {
			n, rerr := res.Body.Read(buf)
			if n > 0 {
				if _, werr := c.Writer.Write(buf[:n]); werr != nil {
					return
				}
				c.Writer.Flush()
			}
			if rerr != nil {
				return
			}
		}
	}
	c.DataFromReader(res.StatusCode, res.ContentLength, res.Header.Get("Content-Type"), res.Body, nil)
}

func streamingMethod(body []byte) bool {
	var rpc struct {
		Method string `json:"method"`
	}
	_ = json.Unmarshal(body, &rpc)
	return rpc.Method == "SendStreamingMessage" || rpc.Method == "SubscribeToTask"
}

// enforceTenant 는 JSON-RPC params.tenant 를 조직과 맞춘다: 없으면 채우고, 다르면 거부 + security.tenant_mismatch 감사.
func (s *Server) enforceTenant(c *gin.Context, body []byte, org, resource string) ([]byte, bool) {
	var rpc map[string]any
	if json.Unmarshal(body, &rpc) != nil {
		return body, true // JSON-RPC 파싱 오류는 에이전트가 표준 오류로 응답
	}
	params, _ := rpc["params"].(map[string]any)
	if params == nil {
		params = map[string]any{}
	}
	if t, ok := params["tenant"].(string); ok && t != "" && t != org {
		s.audit(c, org, "security.tenant_mismatch", resource, "denied", nil, nil, map[string]any{"requested": t, "org": org})
		s.emit(c, "plt.security.tenant_mismatch", gin.H{"org": org, "requested": t, "resource": resource})
		fail(c, http.StatusForbidden, "tenant_mismatch", "요청 tenant 가 조직 문맥과 다름", gin.H{"org": org, "tenant": t})
		return nil, false
	}
	params["tenant"] = org
	rpc["params"] = params
	out, _ := json.Marshal(rpc)
	return out, true
}

func mergeBaggage(existing, kv string) string {
	if existing == "" {
		return kv
	}
	var keep []string
	for _, m := range strings.Split(existing, ",") {
		if !strings.HasPrefix(strings.TrimSpace(m), "atlas.tenant=") {
			keep = append(keep, strings.TrimSpace(m))
		}
	}
	return strings.Join(append(keep, kv), ",")
}
