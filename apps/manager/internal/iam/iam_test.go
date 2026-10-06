package iam

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"atlas-manager/internal/store"
)

func TestULIDSortableUnique(t *testing.T) {
	a := ULID()
	time.Sleep(2 * time.Millisecond)
	b := ULID()
	if len(a) != 26 || len(b) != 26 || a >= b {
		t.Fatalf("ULID not 26-char time-sortable: %s %s", a, b)
	}
}

func TestAPIKeyFormatAndVerify(t *testing.T) {
	plain, prefix, salt, hash := NewAPIKey("prod")
	if !strings.HasPrefix(plain, prefix+"_") || !strings.HasPrefix(prefix, "atl_prod_") {
		t.Fatalf("format: %s / %s", plain, prefix)
	}
	p, sec, err := SplitAPIKey(plain)
	if err != nil || p != prefix || !VerifyAPIKey(sec, salt, hash) {
		t.Fatal("verify own key")
	}
	if VerifyAPIKey(sec+"x", salt, hash) {
		t.Fatal("tampered secret accepted")
	}
	if _, _, err := SplitAPIKey("sk-atlas-local-xyz"); err == nil {
		t.Fatal("foreign key format accepted")
	}
	if strings.Contains(hash, sec) {
		t.Fatal("hash must not contain secret")
	}
}

func TestSignerRoundTripAndJWKS(t *testing.T) {
	s, eph, err := NewSigner("", "atlas-manager")
	if err != nil || !eph {
		t.Fatal(err)
	}
	tok, _ := s.Mint("user-1", "agent:x", "acme", "web", "prod", time.Minute)
	c, err := s.Parse(tok)
	if err != nil || c.Org != "acme" || c.Proj != "web" || c.Env != "prod" || c.Subject != "user-1" {
		t.Fatalf("claims: %+v %v", c, err)
	}
	other, _, _ := NewSigner("", "atlas-manager")
	if _, err := other.Parse(tok); err == nil {
		t.Fatal("token verified with another key")
	}
	if _, eph, _ := NewSigner("CHANGE_ME", "x"); !eph {
		t.Fatal("CHANGE_ME must fall back to ephemeral")
	}
	k := s.JWKS()["keys"].([]map[string]string)[0]
	if k["kid"] != s.Kid || k["crv"] != "P-256" || len(k["x"]) != 43 {
		t.Fatalf("jwks: %v", k)
	}
}

func TestSignerBase64PEM(t *testing.T) {
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	der, _ := x509.MarshalECPrivateKey(key)
	pemStr := string(pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: der}))
	a, eph1, err1 := NewSigner(pemStr, "x")
	b, eph2, err2 := NewSigner(base64.StdEncoding.EncodeToString([]byte(pemStr)), "x")
	if err1 != nil || err2 != nil || eph1 || eph2 || a.Kid != b.Kid {
		t.Fatalf("pem vs base64: %v %v %v %v", err1, err2, eph1, eph2)
	}
}

func TestModelShape(t *testing.T) {
	m := Model()
	if m["schema_version"] != "1.1" || len(m["type_definitions"].([]any)) != 6 {
		t.Fatalf("model: %v", m)
	}
	for _, r := range []string{"org-owner", "agent-developer", "deployer", "env-deployer", "auditor"} {
		if _, ok := Roles[r]; !ok {
			t.Fatalf("role template missing: %s", r)
		}
	}
}

func TestAuditChainAppendOnlyAndTamperDetect(t *testing.T) {
	db, err := store.Open("sqlite", "file:"+filepath.Join(t.TempDir(), "a.db"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Migrate(db, "sqlite"); err != nil {
		t.Fatal(err)
	}
	a := &Audit{DB: db, Engine: "sqlite"}
	ctx := context.Background()
	for i := 0; i < 5; i++ {
		if _, err := a.Append(ctx, Entry{Org: "acme", ActorType: "user", ActorID: "alice", Action: "agent.create", Resource: "agent:1", After: i}); err != nil {
			t.Fatal(err)
		}
	}
	_, _ = a.Append(ctx, Entry{Org: "beta", ActorType: "user", ActorID: "bob", Action: "agent.create"})
	n, broken, _ := a.Verify(ctx, "acme")
	if n != 5 || broken != 0 {
		t.Fatalf("chain: n=%d broken=%d", n, broken)
	}
	if err := db.Exec("UPDATE audit_events SET action = 'x' WHERE seq = 2").Error; err == nil {
		t.Fatal("UPDATE must be rejected by trigger")
	}
	if err := db.Exec("DELETE FROM audit_events").Error; err == nil {
		t.Fatal("DELETE must be rejected by trigger")
	}
	// 트리거를 우회한 변조(직접 삽입)도 체인 검증이 잡는다
	db.Exec("DROP TRIGGER audit_no_update")
	db.Exec("UPDATE audit_events SET resource = 'agent:evil' WHERE org_id = 'acme' AND seq = 3")
	if _, broken, _ := a.Verify(ctx, "acme"); broken != 3 {
		t.Fatalf("tamper not detected at seq 3: %d", broken)
	}
	root, cnt, _ := a.Root(ctx, "beta", time.Now().UTC())
	if cnt != 1 || len(root) != 64 {
		t.Fatalf("merkle root: %s %d", root, cnt)
	}
}

func TestModelABACConditions(t *testing.T) {
	raw, _ := json.Marshal(Model())
	s := string(raw)
	for _, want := range []string{`"business_hours"`, `"ip.in_cidr(cidr)"`, `{"condition":"mfa_verified","type":"user"}`, `{"condition":"amount_limit","type":"user"}`} {
		if !strings.Contains(s, want) {
			t.Fatalf("model missing %s", want)
		}
	}
	if AcrLevel("2") != 2 || AcrLevel("1") != 1 || AcrLevel("mfa") != 2 || AcrLevel("") != 0 {
		t.Fatal("acr levels")
	}
	if SpiffeID("acme", "p", "prod", "a") != "spiffe://atlas/acme/p/prod/a" {
		t.Fatal("spiffe")
	}
}
