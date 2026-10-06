package capture

import (
	"strings"
	"testing"
)

func TestScrubSecrets(t *testing.T) {
	in := "keys sk-atlas-local-devAlPlanKey0000000000 AKIAABCDEFGHIJKLMNOP ghp_" + strings.Repeat("a", 36) + " sk-proj-abcdefghijklmnopqrstuv ok"
	out, n := Scrub(in)
	if n != 4 || strings.Contains(out, "AKIA") || strings.Contains(out, "ghp_") || strings.Contains(out, "sk-") || !strings.HasSuffix(out, " ok") {
		t.Fatalf("n=%d out=%s", n, out)
	}
	if _, _, ok := Body("metadata", "x"); ok {
		t.Fatal("metadata must not capture body")
	}
	b, n, ok := Body("redacted", []map[string]string{{"role": "user", "content": "token AKIAABCDEFGHIJKLMNOP"}})
	if !ok || n != 1 || strings.Contains(b, "AKIA") {
		t.Fatalf("body=%s n=%d", b, n)
	}
}
