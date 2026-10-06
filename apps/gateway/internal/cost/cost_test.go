package cost

import (
	"testing"
	"time"

	"atlas-gateway/internal/config"
)

func TestCostIsExactAndVersioned(t *testing.T) {
	tb, err := NewTable(map[string][]config.PriceVersion{"m": {
		{EffectiveFrom: "2026-01-01", Input: "0.50", Output: "1.50"},
		{EffectiveFrom: "2026-07-01", Input: "0.40", Output: "1.20"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	old, _ := tb.Cost("m", 1000, 333, time.Date(2026, 3, 1, 0, 0, 0, 0, time.UTC))
	if got := Format(old); got != "0.00099950" { // (1000*0.5 + 333*1.5)/1e6
		t.Fatalf("old price cost = %s", got)
	}
	cur, _ := tb.Cost("m", 1000, 333, time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC))
	if got := Format(cur); got != "0.00079960" {
		t.Fatalf("new price cost = %s", got)
	}
	if E8(cur) != 79960 {
		t.Fatalf("E8 = %d", E8(cur))
	}
	if _, known := tb.Cost("unknown", 1, 1, time.Now()); known {
		t.Fatal("unknown model priced")
	}
}
