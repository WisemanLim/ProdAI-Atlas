// Package ui 는 콘솔 SPA 를 Manager 바이너리에 내장해 서빙한다 (FR-PLT-CON-01, PRD §16.1 "Manager embed.FS").
// 번들 빌드는 apps/console 의 dist 를 이 패키지의 dist/ 로 복사한 뒤 go build 한다. 기본 dist/ 는 안내 페이지만 담는다.
package ui

import (
	"embed"
	"io/fs"
	"net/http"
	"path"
	"strings"

	"github.com/gin-gonic/gin"
)

//go:embed all:dist
var dist embed.FS

// Handler 는 정적 파일을 주고, 없는 경로는 index.html 로 돌려 SPA 라우팅을 살린다. API 경로는 건드리지 않는다.
func Handler() gin.HandlerFunc {
	sub, _ := fs.Sub(dist, "dist")
	files := http.FileServer(http.FS(sub))
	return func(c *gin.Context) {
		p := c.Request.URL.Path
		if c.Request.Method != http.MethodGet && c.Request.Method != http.MethodHead ||
			strings.HasPrefix(p, "/api/") || strings.HasPrefix(p, "/svc/") || p == "/health" {
			c.JSON(http.StatusNotFound, gin.H{"error": "not_found", "message": "경로 없음"})
			return
		}
		clean := strings.TrimPrefix(path.Clean(p), "/")
		if clean == "" {
			clean = "index.html"
		}
		if _, err := fs.Stat(sub, clean); err != nil {
			c.Request.URL.Path = "/" // SPA fallback
		} else if strings.HasPrefix(clean, "assets/") {
			c.Header("Cache-Control", "public, max-age=31536000, immutable") // 해시 파일명
		}
		files.ServeHTTP(c.Writer, c.Request)
	}
}

// Bundled 는 실제 콘솔 번들이 들어 있는지(안내 페이지가 아닌지) 알려 준다.
func Bundled() bool {
	_, err := fs.Stat(dist, "dist/assets")
	return err == nil
}
