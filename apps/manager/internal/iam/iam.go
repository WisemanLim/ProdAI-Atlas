// Package iam 은 Manager 인증·인가·감사다 (PRD §11 PLT-IAM · PLT-TEN):
// OIDC(Keycloak) 토큰 검증 · 개인/서비스 API 키(Argon2id) · OpenFGA RBAC · A2A 테넌트 JWT 서명 · 해시 체인 감사 로그.
package iam

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"math/big"
	"time"
)

// Principal 은 요청 주체다.
type Principal struct {
	Kind          string   `json:"kind"` // user | service | apikey | anonymous
	Sub           string   `json:"sub"`
	Name          string   `json:"name,omitempty"`
	Email         string   `json:"email,omitempty"`
	Org           string   `json:"org,omitempty"` // 토큰 atlas_org 클레임 · API 키 조직
	PlatformAdmin bool     `json:"platformAdmin"`
	Scopes        []string `json:"scopes,omitempty"` // API 키 범위
	KeyID         string   `json:"keyId,omitempty"`
	// 세션 (FR-PLT-IAM-09): OIDC acr·sid·인증 시각·발급 시각
	Acr      int       `json:"acr,omitempty"`
	Sid      string    `json:"sid,omitempty"`
	AuthTime time.Time `json:"authTime,omitzero"`
	IssuedAt time.Time `json:"issuedAt,omitzero"`
	Actor    string    `json:"actor,omitempty"` // 위임 토큰의 행위자 (RFC 8693 act.sub)
}

// FGAUser 는 OpenFGA 주체 표기다.
func (p Principal) FGAUser() string { return "user:" + p.Sub }

// HasScope 는 API 키 범위 검사다("admin" 은 전부).
func (p Principal) HasScope(s string) bool {
	for _, x := range p.Scopes {
		if x == s || x == "admin" {
			return true
		}
	}
	return false
}

type ctxKey struct{}

func With(ctx context.Context, p Principal) context.Context {
	return context.WithValue(ctx, ctxKey{}, p)
}

func From(ctx context.Context) (Principal, bool) {
	p, ok := ctx.Value(ctxKey{}).(Principal)
	return p, ok
}

// ULID 는 Crockford base32 ULID(26자 = 48비트 ms 시각 + 80비트 난수, 시간 정렬)다.
func ULID() string {
	const enc = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
	var b [16]byte
	binary.BigEndian.PutUint64(b[:8], uint64(time.Now().UnixMilli())<<16)
	_, _ = rand.Read(b[6:])
	n := new(big.Int).SetBytes(b[:])
	out := make([]byte, 26)
	mask := big.NewInt(31)
	for i := 25; i >= 0; i-- {
		out[i] = enc[new(big.Int).And(n, mask).Int64()]
		n.Rsh(n, 5)
	}
	return string(out)
}
