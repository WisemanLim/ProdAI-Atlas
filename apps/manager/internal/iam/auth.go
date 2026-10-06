package iam

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"strconv"
	"strings"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/golang-jwt/jwt/v5"
	"golang.org/x/crypto/argon2"
)

// ---- OIDC (FR-PLT-IAM-01): Keycloak 발급 토큰만 신뢰(단일 검증 경로) ----

type OIDC struct {
	Issuer   string
	Audience string // 비면 aud 검사 생략(azp 로 클라이언트 확인)
	verifier *oidc.IDTokenVerifier
}

// NewOIDC 는 discovery 없이 JWKS URL 로 검증기를 만든다 — 브라우저가 보는 issuer(호스트 포트)와
// 컨테이너가 닿는 JWKS 주소(내부 DNS)가 다른 compose 배치를 위해서다.
func NewOIDC(ctx context.Context, issuer, jwksURL, audience string) *OIDC {
	ks := oidc.NewRemoteKeySet(ctx, jwksURL)
	cfg := &oidc.Config{ClientID: audience, SkipClientIDCheck: audience == "", SupportedSigningAlgs: []string{"RS256", "ES256"}}
	return &OIDC{Issuer: issuer, Audience: audience, verifier: oidc.NewVerifier(issuer, ks, cfg)}
}

type kcClaims struct {
	Sub               string                   `json:"sub"`
	PreferredUsername string                   `json:"preferred_username"`
	Email             string                   `json:"email"`
	AtlasOrg          string                   `json:"atlas_org"`
	Azp               string                   `json:"azp"`
	ClientID          string                   `json:"client_id"`
	RealmAccess       struct{ Roles []string } `json:"realm_access"`
	Acr               string                   `json:"acr"`
	Sid               string                   `json:"sid"`
	AuthTime          int64                    `json:"auth_time"`
	Iat               int64                    `json:"iat"`
}

// AcrLevel 은 acr 클레임 → 단계다. Keycloak LoA 는 숫자("0"·"1"·"2"), 그 외 이름은 mfa 계열만 2.
func AcrLevel(acr string) int {
	if n, err := strconv.Atoi(acr); err == nil {
		return n
	}
	switch strings.ToLower(acr) {
	case "mfa", "otp", "webauthn", "urn:mace:incommon:iap:silver", "phr", "phrh":
		return 2
	}
	return 0
}

func (o *OIDC) Verify(ctx context.Context, raw string) (Principal, error) {
	tok, err := o.verifier.Verify(ctx, raw)
	if err != nil {
		return Principal{}, err
	}
	var c kcClaims
	if err := tok.Claims(&c); err != nil {
		return Principal{}, err
	}
	p := Principal{Kind: "user", Sub: c.Sub, Name: c.PreferredUsername, Email: c.Email, Org: c.AtlasOrg, Acr: AcrLevel(c.Acr), Sid: c.Sid,
		IssuedAt: time.Unix(c.Iat, 0).UTC()}
	if c.AuthTime > 0 {
		p.AuthTime = time.Unix(c.AuthTime, 0).UTC()
	} else {
		p.AuthTime = p.IssuedAt
	}
	if c.ClientID != "" || strings.HasPrefix(c.PreferredUsername, "service-account-") {
		p.Kind = "service" // client credentials (FR-PLT-IAM-06)
	}
	for _, r := range c.RealmAccess.Roles {
		if r == "platform-admin" {
			p.PlatformAdmin = true
		}
	}
	return p, nil
}

// ---- API 키 (FR-PLT-IAM-06): atl_{env}_{ulid}_{secret}, Argon2id 해시 ----

var ErrBadKey = errors.New("invalid api key")

// NewAPIKey 는 평문 키와 (prefix, salt, hash) 를 만든다. 평문은 호출자에게 한 번만 보인다.
func NewAPIKey(env string) (plain, prefix, salt, hash string) {
	sec := make([]byte, 24)
	_, _ = rand.Read(sec)
	s := make([]byte, 16)
	_, _ = rand.Read(s)
	prefix = fmt.Sprintf("atl_%s_%s", env, strings.ToLower(ULID()))
	secret := base64.RawURLEncoding.EncodeToString(sec)
	salt = hex.EncodeToString(s)
	return prefix + "_" + secret, prefix, salt, argonHash(secret, salt)
}

// HashSecret 은 새 salt 로 비밀을 해시한다 (부트스트랩 키 upsert 용).
func HashSecret(secret string) (salt, hash string) {
	s := make([]byte, 16)
	_, _ = rand.Read(s)
	salt = hex.EncodeToString(s)
	return salt, argonHash(secret, salt)
}

func argonHash(secret, salt string) string {
	s, _ := hex.DecodeString(salt)
	return hex.EncodeToString(argon2.IDKey([]byte(secret), s, 2, 19*1024, 1, 32)) // OWASP 권고 최소(m=19MiB, t=2, p=1)
}

// SplitAPIKey 는 "atl_{env}_{ulid}_{secret}" → (prefix, secret).
func SplitAPIKey(plain string) (string, string, error) {
	parts := strings.SplitN(plain, "_", 4)
	if len(parts) != 4 || parts[0] != "atl" || len(parts[2]) != 26 || parts[3] == "" {
		return "", "", ErrBadKey
	}
	return strings.Join(parts[:3], "_"), parts[3], nil
}

func VerifyAPIKey(secret, salt, hash string) bool {
	return subtle.ConstantTimeCompare([]byte(argonHash(secret, salt)), []byte(hash)) == 1
}

// ParseScopes 는 공백·쉼표 구분 범위를 정규화한다.
func ParseScopes(s string) []string {
	var out []string
	for _, f := range strings.FieldsFunc(s, func(r rune) bool { return r == ' ' || r == ',' }) {
		out = append(out, f)
	}
	return out
}

// KnownScopes 는 API 키 범위다.
var KnownScopes = map[string]bool{"read": true, "agents:write": true, "deployments:write": true, "a2a:invoke": true, "audit:read": true, "svc": true, "secrets:write": true, "admin": true,
	"scim": true} // scim = 조직 SCIM 프로비저닝 Bearer 토큰 (FR-PLT-IAM-03)

// ---- A2A 테넌트 JWT (FR-PLT-TEN-06): Manager 가 ES256 으로 서명, 에이전트는 /.well-known/jwks.json 으로 검증 ----

type Signer struct {
	key *ecdsa.PrivateKey
	Kid string
	Iss string
}

// NewSigner 는 PEM(EC P-256) 이 있으면 쓰고, 없으면 임시 키를 만든다(단일 인스턴스 전제 — 운영은 비밀 주입).
func NewSigner(pemData, issuer string) (*Signer, bool, error) {
	var key *ecdsa.PrivateKey
	ephemeral := false
	if strings.Contains(pemData, "CHANGE_ME") {
		pemData = ""
	}
	if raw := strings.TrimSpace(pemData); raw != "" && !strings.HasPrefix(raw, "-----") {
		if dec, err := base64.StdEncoding.DecodeString(raw); err == nil { // base64(PEM) — .env 한 줄 주입용
			pemData = string(dec)
		}
	}
	if strings.TrimSpace(pemData) != "" {
		blk, _ := pem.Decode([]byte(pemData))
		if blk == nil {
			return nil, false, errors.New("signing key: PEM 아님")
		}
		k, err := x509.ParseECPrivateKey(blk.Bytes)
		if err != nil {
			pk, err2 := x509.ParsePKCS8PrivateKey(blk.Bytes)
			ek, ok := pk.(*ecdsa.PrivateKey)
			if err2 != nil || !ok {
				return nil, false, fmt.Errorf("signing key: %w", err)
			}
			k = ek
		}
		key = k
	} else {
		k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if err != nil {
			return nil, false, err
		}
		key, ephemeral = k, true
	}
	pub, _ := x509.MarshalPKIXPublicKey(&key.PublicKey)
	h := sha256.Sum256(pub)
	return &Signer{key: key, Kid: hex.EncodeToString(h[:8]), Iss: issuer}, ephemeral, nil
}

// TenantClaims 는 A2A 호출에 실리는 테넌트 클레임이다.
type TenantClaims struct {
	Org  string `json:"atlas_org"`
	Proj string `json:"atlas_proj"`
	Env  string `json:"atlas_env"`
	// 워크로드 신원 (FR-PLT-IAM-07): SPIFFE 형식 ID · 위임(RFC 8693) 행위자·축소 범위
	SpiffeID string `json:"spiffe_id,omitempty"`
	Act      *Actor `json:"act,omitempty"`
	Scope    string `json:"scope,omitempty"`
	jwt.RegisteredClaims
}

type Actor struct {
	Sub string `json:"sub"`
}

// SpiffeID 는 spiffe://atlas/{org}/{proj}/{env}/{agent} 다.
func SpiffeID(org, proj, env, agent string) string {
	return "spiffe://atlas/" + org + "/" + proj + "/" + env + "/" + agent
}

func (s *Signer) Mint(sub, aud, org, proj, env string, ttl time.Duration) (string, error) {
	return s.MintClaims(TenantClaims{Org: org, Proj: proj, Env: env, RegisteredClaims: jwt.RegisteredClaims{Subject: sub, Audience: jwt.ClaimStrings{aud}}}, ttl)
}

// MintClaims 는 발급자·시각·jti 를 채워 서명한다.
func (s *Signer) MintClaims(c TenantClaims, ttl time.Duration) (string, error) {
	now := time.Now()
	c.Issuer, c.IssuedAt, c.ExpiresAt, c.ID = s.Iss, jwt.NewNumericDate(now), jwt.NewNumericDate(now.Add(ttl)), ULID()
	t := jwt.NewWithClaims(jwt.SigningMethodES256, c)
	t.Header["kid"] = s.Kid
	return t.SignedString(s.key)
}

func (s *Signer) Parse(raw string) (TenantClaims, error) {
	var c TenantClaims
	_, err := jwt.ParseWithClaims(raw, &c, func(t *jwt.Token) (any, error) { return &s.key.PublicKey, nil },
		jwt.WithValidMethods([]string{"ES256"}), jwt.WithIssuer(s.Iss))
	return c, err
}

// SignJWS 는 detached 가 아닌 일반 JWS 의 (protected, signature) 를 만든다 — A2A AgentCardSignature 형식.
func (s *Signer) SignJWS(payload []byte) (protected, signature string, err error) {
	hdr, _ := json.Marshal(map[string]string{"alg": "ES256", "kid": s.Kid, "typ": "JOSE"})
	protected = base64.RawURLEncoding.EncodeToString(hdr)
	sig, err := jwt.SigningMethodES256.Sign(protected+"."+base64.RawURLEncoding.EncodeToString(payload), s.key)
	if err != nil {
		return "", "", err
	}
	return protected, base64.RawURLEncoding.EncodeToString(sig), nil
}

// VerifyJWS 는 SignJWS 결과를 검증한다(시험·외부 카드 검증용).
func (s *Signer) VerifyJWS(protected, signature string, payload []byte) error {
	sig, err := base64.RawURLEncoding.DecodeString(signature)
	if err != nil {
		return err
	}
	return jwt.SigningMethodES256.Verify(protected+"."+base64.RawURLEncoding.EncodeToString(payload), sig, &s.key.PublicKey)
}

// JWKS 는 공개키 집합이다.
func (s *Signer) JWKS() map[string]any {
	b64 := func(n *big.Int) string {
		buf := make([]byte, 32)
		n.FillBytes(buf)
		return base64.RawURLEncoding.EncodeToString(buf)
	}
	return map[string]any{"keys": []map[string]string{{"kty": "EC", "crv": "P-256", "alg": "ES256", "use": "sig", "kid": s.Kid,
		"x": b64(s.key.PublicKey.X), "y": b64(s.key.PublicKey.Y)}}}
}
