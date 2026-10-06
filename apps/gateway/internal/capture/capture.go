// Package capture 는 span 본문 기록 정책(FR-OB-LOG-01)과 비밀 누출 차단(FR-OB-LOG-03)이다.
// 모드: none(본문·메타 없음) · metadata(본문 없음) · redacted(비밀 제거, PII 는 가드·Collector 2단 정화) · full(비밀만 제거).
package capture

import (
	"encoding/json"
	"regexp"
)

const maxBytes = 16 << 10 // span 속성 크기 상한

var secretPatterns = []*regexp.Regexp{
	regexp.MustCompile(`sk-atlas-[a-z]+-[0-9A-Za-z]{22}`),
	regexp.MustCompile(`\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}`),
	regexp.MustCompile(`\bAKIA[0-9A-Z]{16}\b`),
	regexp.MustCompile(`\bgh[pousr]_[A-Za-z0-9]{36,}\b`),
	regexp.MustCompile(`\bxox[abpr]-[A-Za-z0-9-]{10,}\b`),
	regexp.MustCompile(`-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----`),
}

// Scrub 은 알려진 비밀 형식을 [SECRET] 로 바꾸고 발견 개수를 돌려준다.
func Scrub(s string) (string, int) {
	n := 0
	for _, rx := range secretPatterns {
		s = rx.ReplaceAllStringFunc(s, func(string) string { n++; return "[SECRET]" })
	}
	return s, n
}

// Body 는 기록할 JSON 본문(비밀 제거·크기 제한)과 비밀 발견 개수다. mode 가 본문을 기록하지 않으면 ok=false.
func Body(mode string, v any) (body string, secrets int, ok bool) {
	if mode != "redacted" && mode != "full" {
		return "", 0, false
	}
	b, _ := json.Marshal(v)
	s, n := Scrub(string(b))
	if len(s) > maxBytes {
		s = s[:maxBytes] + `…(truncated)`
	}
	return s, n, true
}
