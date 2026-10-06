package manifest

import (
	"errors"
	"strings"
	"testing"
)

const valid = `{"apiVersion":"agents.prodai.io/v1alpha1","kind":"AtlasAgent",
 "metadata":{"name":"al-plan","unitId":"AL-PLAN"},
 "spec":{"version":"0.1.0","interfaces":[{"binding":"JSONRPC","port":8000,"path":"/a2a"}],"drivers":{"supported":["compose"]}}}`

func TestParseValidFillsCatalogDefaults(t *testing.T) {
	m, err := Parse([]byte(valid))
	if err != nil {
		t.Fatal(err)
	}
	if m.Metadata.Layer != "L1" || m.Metadata.Concept != "01-agentic-loops" {
		t.Fatalf("defaults not filled: %+v", m.Metadata)
	}
	if !strings.Contains(m.JSON(), `"concept":"01-agentic-loops"`) {
		t.Fatal("raw json not updated")
	}
}

func TestParseRejectsInvalidFields(t *testing.T) {
	bad := `{"apiVersion":"v0","kind":"X","metadata":{"name":"Bad_Name","unitId":"NOPE"},
	 "spec":{"version":"1","artifacts":{"image":"ghcr.io/x:latest"},"interfaces":[{"binding":"SOAP","port":0}],"drivers":{"supported":["nomad"]}}}`
	_, err := Parse([]byte(bad))
	var fe Errors
	if !errors.As(err, &fe) {
		t.Fatalf("want Errors, got %v", err)
	}
	want := []string{"apiVersion", "kind", "metadata.name", "metadata.unitId", "spec.version",
		"spec.artifacts.image", "spec.interfaces[0].binding", "spec.interfaces[0].port", "spec.drivers.supported[0]"}
	got := map[string]bool{}
	for _, e := range fe {
		got[e.Field] = true
	}
	for _, f := range want {
		if !got[f] {
			t.Errorf("missing error for %s (got %v)", f, fe)
		}
	}
}

func TestSpecHashStableAcrossKeyOrder(t *testing.T) {
	a, _ := Parse([]byte(valid))
	b, _ := Parse([]byte(`{"kind":"AtlasAgent","apiVersion":"agents.prodai.io/v1alpha1",
	 "spec":{"drivers":{"supported":["compose"]},"interfaces":[{"path":"/a2a","port":8000,"binding":"JSONRPC"}],"version":"0.1.0"},
	 "metadata":{"unitId":"AL-PLAN","name":"al-plan"}}`))
	if a.SpecHash() != b.SpecHash() {
		t.Fatal("hash depends on key order")
	}
}

func TestSecretsDeclaration(t *testing.T) {
	ok := `{"apiVersion":"agents.prodai.io/v1alpha1","kind":"AtlasAgent","metadata":{"name":"gr","unitId":"GR-IO"},
 "spec":{"version":"0.1.0","interfaces":[{"binding":"JSONRPC","port":8000}],"secrets":[{"name":"guard-secret","required":true}]}}`
	m, err := Parse([]byte(ok))
	if err != nil || !m.SecretNames()["guard-secret"] {
		t.Fatalf("secrets: %v %v", m, err)
	}
	bad := strings.Replace(ok, `"guard-secret"`, `"Bad Name"`, 1)
	if _, err := Parse([]byte(bad)); err == nil || !strings.Contains(err.Error(), "spec.secrets[0].name") {
		t.Fatalf("bad name accepted: %v", err)
	}
}
