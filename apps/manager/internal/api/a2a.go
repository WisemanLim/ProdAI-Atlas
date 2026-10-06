package api

import (
	"bytes"
	"encoding/json"
	"math"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"unicode"

	"github.com/gin-gonic/gin"

	"atlas-manager/internal/manifest"
	"atlas-manager/internal/store"
)

// A2A 카드 게시·서명·검색 (FR-A2A-CARD-01·02·03·06).

// visibility 는 매니페스트 spec.agentCard.visibility: private(같은 프로젝트) · tenant(같은 조직, 기본) · public(모든 조직).
func visibility(m *manifest.Manifest) string {
	if m != nil {
		if ac, ok := m.Raw["spec"].(map[string]any)["agentCard"].(map[string]any); ok {
			if v, ok := ac["visibility"].(string); ok && (v == "private" || v == "public" || v == "tenant") {
				return v
			}
		}
	}
	return "tenant"
}

// Canonical 은 JCS(RFC 8785) 정규화다: 키 사전순(encoding/json 의 map 정렬), 공백 없음, HTML 이스케이프 없음.
func Canonical(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	// 구조체를 map 으로 한 번 풀어 키 순서를 정렬한다
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var generic any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&generic); err != nil {
		return nil, err
	}
	if err := enc.Encode(generic); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// signedCard 는 저장된 카드(배포 시 수집)에 Atlas 메타데이터를 더하고 JWS(ES256, kid) 서명을 붙인다.
func (s *Server) signedCard(a store.Agent, d store.Deployment, vis string) (map[string]any, error) {
	card := map[string]any{}
	if d.Card != nil {
		_ = json.Unmarshal([]byte(*d.Card), &card)
	}
	delete(card, "signatures")
	md, _ := card["metadata"].(map[string]any)
	if md == nil {
		md = map[string]any{}
	}
	md["atlas.agentId"], md["atlas.org"], md["atlas.project"], md["atlas.env"], md["atlas.visibility"] = a.ID, a.OrgID, a.Project, d.Env, vis
	card["metadata"] = md
	if s.Signer == nil {
		return card, nil
	}
	payload, err := Canonical(card)
	if err != nil {
		return nil, err
	}
	protected, sig, err := s.Signer.SignJWS(payload)
	if err != nil {
		return nil, err
	}
	card["signatures"] = []map[string]any{{"protected": protected, "signature": sig}}
	return card, nil
}

// canSee 는 카드 가시성 검사다: 같은 조직은 private 이면 프로젝트 viewer, 아니면 조직 viewer(에이전트 주체는 같은 조직), public 은 인증된 누구나.
func (s *Server) canSee(c *gin.Context, a store.Agent, vis string) bool {
	p := principal(c)
	if vis == "public" && (p.Kind != "anonymous" || !s.authOn()) {
		return true
	}
	if p.Kind == "agent" {
		return p.Org == a.OrgID && vis != "private"
	}
	if vis == "private" {
		return s.can(c, "viewer", projObj(a.OrgID, a.Project))
	}
	return s.can(c, "viewer", orgObj(a.OrgID)) || s.can(c, "viewer", projObj(a.OrgID, a.Project))
}

func (s *Server) readyDeployment(c *gin.Context, agentID, env string) (store.Deployment, bool) {
	d, err := pickDeployment(s.DB.WithContext(c), agentID, env, "")
	return d, err == nil
}

// getCard: GET /api/v1/agents/{id}/card — 서명된 카드 (조직 문맥 밖 public 카드도 조회 가능).
func (s *Server) getCard(c *gin.Context) {
	var a store.Agent
	if err := s.DB.WithContext(c).First(&a, "id = ?", c.Param("id")).Error; err != nil {
		fail(c, http.StatusNotFound, "not_found", "에이전트 없음", nil)
		return
	}
	var rev store.Revision
	s.DB.WithContext(c).First(&rev, "agent_id = ? AND rev = ?", a.ID, a.CurrentRev)
	m, _ := manifest.Parse([]byte(rev.Manifest))
	vis := visibility(m)
	if !s.canSee(c, a, vis) {
		fail(c, http.StatusNotFound, "not_found", "에이전트 없음", nil) // 존재 자체를 숨긴다
		return
	}
	d, ok := s.readyDeployment(c, a.ID, c.DefaultQuery("env", "dev"))
	if !ok || d.Card == nil {
		fail(c, http.StatusServiceUnavailable, "no_ready_deployment", "ready 배포의 카드 없음", nil)
		return
	}
	card, err := s.signedCard(a, d, vis)
	if err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	c.JSON(http.StatusOK, card)
}

// ---- skill 검색 레지스트리 (BM25, FR-A2A-CARD-03) ----

func tokens(s string) []string {
	var out []string
	for _, f := range strings.FieldsFunc(strings.ToLower(s), func(r rune) bool { return !unicode.IsLetter(r) && !unicode.IsDigit(r) }) {
		if len([]rune(f)) > 1 {
			out = append(out, f)
		}
	}
	return out
}

type regEntry struct {
	AgentID    string           `json:"agentId"`
	Name       string           `json:"name"`
	Org        string           `json:"org"`
	Project    string           `json:"project"`
	UnitID     string           `json:"unitId"`
	Visibility string           `json:"visibility"`
	Skills     []map[string]any `json:"skills"`
	Score      float64          `json:"score"`
	CardURL    string           `json:"cardUrl"`
	Trust      string           `json:"trust"`
	terms      []string
}

// registry: GET /api/v1/a2a/registry/agents?q=&skill=&tag=&mode=&limit= — 볼 수 있는 ready 에이전트만, 쿼리가 있으면 BM25 순.
func (s *Server) registry(c *gin.Context) {
	var agents []store.Agent
	if err := s.DB.WithContext(c).Find(&agents).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	env := c.DefaultQuery("env", "dev")
	skillQ, tagQ, modeQ := c.Query("skill"), strings.ToLower(c.Query("tag")), c.Query("mode")
	var docs []*regEntry
	for _, a := range agents {
		var rev store.Revision
		s.DB.WithContext(c).First(&rev, "agent_id = ? AND rev = ?", a.ID, a.CurrentRev)
		m, _ := manifest.Parse([]byte(rev.Manifest))
		vis := visibility(m)
		if !s.canSee(c, a, vis) {
			continue
		}
		d, ok := s.readyDeployment(c, a.ID, env)
		if !ok || d.Card == nil {
			continue
		}
		var card struct {
			Description string           `json:"description"`
			Skills      []map[string]any `json:"skills"`
		}
		_ = json.Unmarshal([]byte(*d.Card), &card)
		e := &regEntry{AgentID: a.ID, Name: a.Name, Org: a.OrgID, Project: a.Project, UnitID: a.UnitID, Visibility: vis,
			CardURL: "/api/v1/agents/" + a.ID + "/card", Trust: map[bool]string{true: "signed", false: "unsigned"}[s.Signer != nil]}
		text := a.Name + " " + a.UnitID + " " + card.Description
		for _, sk := range card.Skills {
			b, _ := json.Marshal(sk)
			if skillQ != "" && sk["id"] != skillQ {
				continue
			}
			if tagQ != "" && !strings.Contains(strings.ToLower(string(b)), `"`+tagQ+`"`) {
				continue
			}
			if modeQ != "" && !strings.Contains(string(b), modeQ) {
				continue
			}
			e.Skills = append(e.Skills, sk)
			text += " " + string(b)
		}
		if len(e.Skills) == 0 {
			continue
		}
		e.terms = tokens(text)
		docs = append(docs, e)
	}
	if q := tokens(c.Query("q")); len(q) > 0 && len(docs) > 0 {
		avg := 0.0
		df := map[string]int{}
		for _, d := range docs {
			avg += float64(len(d.terms))
			seen := map[string]bool{}
			for _, t := range d.terms {
				if !seen[t] {
					seen[t], df[t] = true, df[t]+1
				}
			}
		}
		avg /= float64(len(docs))
		n := float64(len(docs))
		for _, d := range docs {
			tf := map[string]int{}
			for _, t := range d.terms {
				tf[t]++
			}
			for _, t := range q {
				if f := float64(tf[t]); f > 0 {
					idf := math.Log(1 + (n-float64(df[t])+0.5)/(float64(df[t])+0.5))
					d.Score += idf * f * 2.2 / (f + 1.2*(0.25+0.75*float64(len(d.terms))/avg))
				}
			}
		}
		filtered := docs[:0]
		for _, d := range docs {
			if d.Score > 0 {
				filtered = append(filtered, d)
			}
		}
		docs = filtered
		sort.SliceStable(docs, func(i, j int) bool { return docs[i].Score > docs[j].Score })
	}
	limit, _ := strconv.Atoi(c.DefaultQuery("limit", "20"))
	if limit > 0 && len(docs) > limit {
		docs = docs[:limit]
	}
	c.JSON(http.StatusOK, gin.H{"items": docs})
}
