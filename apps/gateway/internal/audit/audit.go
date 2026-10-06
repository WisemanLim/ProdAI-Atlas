// Package audit 은 변조 탐지 가능한 감사 로그다 (FR-GW-POL-04): JSON Lines + SHA-256 해시 체인(prev_hash).
// M0: 인스턴스별 체인을 stdout(+선택 파일)에 쓴다. 일 단위 머클 루트·ClickHouse 적재는 M1.
package audit

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"sync"
	"time"
)

type Event struct {
	TS          time.Time         `json:"ts"`
	Actor       string            `json:"actor"` // 키 prefix
	Tenant      string            `json:"tenant"`
	Action      string            `json:"action"`   // chat.completions
	Resource    string            `json:"resource"` // alias
	Decision    string            `json:"decision"` // allow | deny | error
	Reasons     []string          `json:"reasons,omitempty"`
	RequestHash string            `json:"request_hash"`
	CostUSD     string            `json:"cost_usd,omitempty"`
	Payload     map[string]any    `json:"payload,omitempty"` // payload_logging 정책에 따름
	Extra       map[string]string `json:"extra,omitempty"`
	PrevHash    string            `json:"prev_hash"`
	Hash        string            `json:"hash"`
}

type Logger struct {
	mu   sync.Mutex
	w    io.Writer
	prev string
}

func New(w io.Writer) *Logger { return &Logger{w: w, prev: "genesis"} }

func digest(e Event) string {
	e.Hash = ""
	b, _ := json.Marshal(e)
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

func (l *Logger) Record(e Event) Event {
	l.mu.Lock()
	defer l.mu.Unlock()
	e.PrevHash = l.prev
	e.Hash = digest(e)
	l.prev = e.Hash
	b, _ := json.Marshal(map[string]any{"audit": e})
	_, _ = l.w.Write(append(b, '\n'))
	return e
}

// Verify 는 JSON Lines({"audit":Event}) 체인을 검증한다. 첫 손상 위치를 돌려준다.
func Verify(r io.Reader) (int, error) {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	prev, n := "genesis", 0
	for sc.Scan() {
		var line struct {
			Audit *Event `json:"audit"`
		}
		if json.Unmarshal(sc.Bytes(), &line) != nil || line.Audit == nil {
			continue // 감사 외 로그 라인
		}
		n++
		e := *line.Audit
		if e.PrevHash != prev || digest(e) != e.Hash {
			return n, fmt.Errorf("chain broken at record %d", n)
		}
		prev = e.Hash
	}
	return n, sc.Err()
}

func RequestHash(body []byte) string {
	s := sha256.Sum256(body)
	return hex.EncodeToString(s[:])
}
