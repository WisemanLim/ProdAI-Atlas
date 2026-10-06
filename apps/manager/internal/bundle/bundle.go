// Package bundle 은 compose 설치 번들(scripts/make-bundle.py 생성본)을 atlas CLI 에 내장한다 (FR-PLT-PKG-07·08).
package bundle

import (
	"embed"
	"encoding/json"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

//go:embed all:files
var files embed.FS

// Version 은 내장 번들 버전이다.
func Version() string {
	b, _ := files.ReadFile("files/VERSION")
	return strings.TrimSpace(string(b))
}

// Compat 은 내장 compat.json 이다.
func Compat() (map[string]any, error) {
	b, err := files.ReadFile("files/compat.json")
	if err != nil {
		return nil, err
	}
	var out map[string]any
	return out, json.Unmarshal(b, &out)
}

// WriteTo 는 번들을 dir 에 쓴다. 기존 파일은 force 가 아니면 보존하고, 쓴 파일 목록을 돌려준다.
func WriteTo(dir string, force bool) ([]string, error) {
	var written []string
	err := fs.WalkDir(files, "files", func(p string, d fs.DirEntry, err error) error {
		if err != nil || p == "files" {
			return err
		}
		rel := strings.TrimPrefix(p, "files/")
		dst := filepath.Join(dir, rel)
		if d.IsDir() {
			return os.MkdirAll(dst, 0o755)
		}
		if _, err := os.Stat(dst); err == nil && !force {
			return nil
		}
		b, err := files.ReadFile(p)
		if err != nil {
			return err
		}
		mode := os.FileMode(0o644)
		if strings.HasSuffix(rel, ".sh") {
			mode = 0o755
		}
		written = append(written, rel)
		return os.WriteFile(dst, b, mode)
	})
	return written, err
}
