package catalog

import "testing"

func TestCatalogHas47UniqueUnits(t *testing.T) {
	seen := map[string]bool{}
	for _, u := range All() {
		if seen[u.ID] {
			t.Fatalf("duplicate unit %s", u.ID)
		}
		seen[u.ID] = true
	}
	if len(seen) != 47 {
		t.Fatalf("units = %d, want 47 (PRD §3.3)", len(seen))
	}
	if _, ok := Get("AL-PLAN"); !ok {
		t.Fatal("AL-PLAN missing")
	}
}
