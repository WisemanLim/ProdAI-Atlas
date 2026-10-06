package audit

import (
	"bytes"
	"strings"
	"testing"
	"time"
)

func TestChainVerifiesAndDetectsTampering(t *testing.T) {
	var buf bytes.Buffer
	l := New(&buf)
	for i := 0; i < 3; i++ {
		l.Record(Event{TS: time.Unix(int64(i), 0).UTC(), Actor: "k", Action: "chat.completions", Decision: "allow", CostUSD: "0.00000100"})
	}
	if n, err := Verify(strings.NewReader("noise line\n" + buf.String())); err != nil || n != 3 {
		t.Fatalf("verify = %d %v", n, err)
	}
	tampered := strings.Replace(buf.String(), `"cost_usd":"0.00000100"`, `"cost_usd":"0.00000000"`, 1)
	if n, err := Verify(strings.NewReader(tampered)); err == nil || n != 1 {
		t.Fatalf("tamper not detected: %d %v", n, err)
	}
}
