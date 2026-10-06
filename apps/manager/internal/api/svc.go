package api

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"

	"github.com/gin-gonic/gin"
)

// ParseServices 는 ATLAS_SVC_URLS="gw=http://gateway:8080,ob=http://agent-ob:8000" 형식을 읽는다.
func ParseServices(spec string) map[string]*url.URL {
	out := map[string]*url.URL{}
	for _, kv := range strings.Split(spec, ",") {
		name, raw, ok := strings.Cut(strings.TrimSpace(kv), "=")
		if !ok || name == "" {
			continue
		}
		if u, err := url.Parse(raw); err == nil && u.Host != "" {
			out[name] = u
		}
	}
	return out
}

// proxyService 는 콘솔이 같은 출처로 플랫폼 서비스(게이트웨이·관측·평가·가드·레지스트리·루프)를 부르게 하는 역방향 프록시다.
// 허용 목록에 있는 서비스만 중계한다. 인증 모드에서는 조회 = 조직 viewer, 변경 = 프로젝트 operator, /admin 경로 = 플랫폼 관리자,
// tenant 는 조직 문맥으로 강제하고 사용자 토큰은 하위 서비스로 넘기지 않는다(토큰 패스스루 금지).
func (s *Server) proxyService(c *gin.Context) {
	target, ok := s.Services[c.Param("name")]
	if !ok {
		fail(c, http.StatusNotFound, "unknown_service", "프록시 대상 서비스 없음", gin.H{"name": c.Param("name")})
		return
	}
	org := orgOf(c)
	if s.authOn() {
		switch {
		case strings.Contains(c.Param("path"), "/admin"):
			if !principal(c).PlatformAdmin {
				fail(c, http.StatusForbidden, "forbidden", "서비스 관리 API 는 플랫폼 관리자만", nil)
				return
			}
		case c.Request.Method == http.MethodGet:
			if !s.require(c, "viewer", orgObj(org)) {
				return
			}
		default:
			if !s.require(c, "operator", projObj(org, firstNonEmpty(c.GetHeader("X-Atlas-Project"), "default"))) {
				return
			}
		}
		if _, ok := s.orgGuard(c, org); !ok {
			return
		}
		q := c.Request.URL.Query()
		if t := q.Get("tenant"); t != "" && t != org {
			s.audit(c, org, "security.tenant_mismatch", "svc:"+c.Param("name"), "denied", nil, nil, map[string]any{"requested": t})
			fail(c, http.StatusForbidden, "tenant_mismatch", "요청 tenant 가 조직 문맥과 다름", nil)
			return
		}
		if c.Request.Body != nil && strings.HasPrefix(c.ContentType(), "application/json") {
			raw, _ := io.ReadAll(io.LimitReader(c.Request.Body, 8<<20))
			var body map[string]any
			if json.Unmarshal(raw, &body) == nil {
				if t, ok := body["tenant"].(string); ok && t != "" && t != org {
					s.audit(c, org, "security.tenant_mismatch", "svc:"+c.Param("name"), "denied", nil, nil, map[string]any{"requested": t})
					fail(c, http.StatusForbidden, "tenant_mismatch", "요청 tenant 가 조직 문맥과 다름", nil)
					return
				}
			}
			c.Request.Body = io.NopCloser(bytes.NewReader(raw))
			c.Request.ContentLength = int64(len(raw))
		}
	}
	rp := httputil.NewSingleHostReverseProxy(target)
	rp.FlushInterval = -1 // SSE(런 이벤트) 즉시 전달
	rp.Director = func(r *http.Request) {
		r.URL.Scheme, r.URL.Host, r.Host = target.Scheme, target.Host, target.Host
		r.URL.Path = singleJoin(target.Path, c.Param("path"))
		r.Header.Del("Cookie")
		if s.authOn() {
			r.Header.Del("Authorization")
			r.Header.Set("X-Atlas-Org", org)
		}
	}
	rp.ErrorHandler = func(w http.ResponseWriter, _ *http.Request, err error) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte(`{"error":"service_unreachable","message":"` + strings.ReplaceAll(err.Error(), `"`, `'`) + `"}`))
	}
	rp.ServeHTTP(c.Writer, c.Request)
}

func singleJoin(a, b string) string {
	return strings.TrimRight(a, "/") + "/" + strings.TrimLeft(b, "/")
}
