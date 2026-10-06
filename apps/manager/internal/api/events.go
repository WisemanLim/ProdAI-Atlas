package api

import (
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
)

// relayEvent 는 런타임 에이전트(NATS 클라이언트 없음)가 플랫폼 이벤트를 내도록 중계한다 — 알림 규칙(X-NTF)·감사가 같은 스트림을 본다.
// 허용 subject: plt.bl.* · plt.ev.* · plt.mat.* · plt.kb.* · plt.sbx.* · plt.mcp.* · plt.mkt.* · plt.rpt.* (런타임 단위 이벤트). 조직은 주체 조직으로 고정.
var relayPrefixes = []string{"plt.bl.", "plt.ev.", "plt.mat.", "plt.kb.", "plt.sbx.", "plt.mcp.", "plt.mkt.", "plt.rpt.", "plt.gr."}

func (s *Server) relayEvent(c *gin.Context) {
	var r struct {
		Subject string         `json:"subject"`
		Data    map[string]any `json:"data"`
	}
	if err := c.ShouldBindJSON(&r); err != nil || r.Subject == "" {
		fail(c, http.StatusBadRequest, "bad_request", "subject·data 필수", nil)
		return
	}
	ok := false
	for _, p := range relayPrefixes {
		if strings.HasPrefix(r.Subject, p) && len(r.Subject) > len(p) && !strings.ContainsAny(r.Subject, "*> ") {
			ok = true
		}
	}
	if !ok {
		fail(c, http.StatusForbidden, "subject_not_allowed", "허용 subject: "+strings.Join(relayPrefixes, ", ")+"…", nil)
		return
	}
	org := orgOf(c)
	if !s.require(c, "viewer", orgObj(org)) {
		return
	}
	if r.Data == nil {
		r.Data = map[string]any{}
	}
	r.Data["org"] = org
	p := principal(c)
	r.Data["source"] = firstNonEmpty(p.Sub, p.Kind)
	s.emit(c, r.Subject, r.Data)
	c.JSON(http.StatusAccepted, gin.H{"subject": r.Subject, "org": org})
}
