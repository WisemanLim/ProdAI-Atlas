// Package auth 는 가상키 발급·검증·폐기다 (FR-GW-AUTH-01·05). 저장은 Redis — 평문은 저장하지 않고 SHA-256 해시 + prefix 만.
package auth

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math/big"
	"path"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

const base62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

var (
	ErrInvalid = errors.New("invalid_api_key")
	ErrExpired = errors.New("expired_api_key")
)

// VirtualKey 속성 (PRD §17.1 VirtualKey).
type VirtualKey struct {
	Prefix    string            `json:"prefix"`
	Tenant    string            `json:"tenant"`
	Project   string            `json:"project"`
	Models    []string          `json:"models"` // alias glob, 예: ["*"], ["planner-*"]
	RPM       int               `json:"rpm"`
	TPM       int               `json:"tpm"`
	ExpiresAt *time.Time        `json:"expiresAt,omitempty"`
	CreatedAt time.Time         `json:"createdAt"`
	Metadata  map[string]string `json:"metadata,omitempty"`
}

// Allows 는 alias 허용 여부다 (glob).
func (k VirtualKey) Allows(alias string) bool {
	for _, g := range k.Models {
		if ok, _ := path.Match(g, alias); ok {
			return true
		}
	}
	return false
}

type Store struct {
	rdb *redis.Client
	env string
}

func NewStore(rdb *redis.Client, env string) *Store { return &Store{rdb, env} }

func hashKey(plain string) string {
	s := sha256.Sum256([]byte(plain))
	return hex.EncodeToString(s[:])
}

func randomBase62(n int) string {
	b := make([]byte, n)
	for i := range b {
		x, _ := rand.Int(rand.Reader, big.NewInt(int64(len(base62))))
		b[i] = base62[x.Int64()]
	}
	return string(b)
}

const (
	hashPrefix = "gw:vk:h:"  // + sha256 → JSON
	indexKey   = "gw:vk:idx" // hash: prefix → sha256
)

// Issue 는 sk-atlas-<env>-<22자 base62> 키를 만들고 평문을 한 번만 돌려준다.
func (s *Store) Issue(ctx context.Context, vk VirtualKey) (string, VirtualKey, error) {
	plain := "sk-atlas-" + s.env + "-" + randomBase62(22)
	return plain, vk, s.Put(ctx, plain, vk)
}

// Put 은 주어진 평문 키를 등록한다(부트스트랩 키 upsert 용).
func (s *Store) Put(ctx context.Context, plain string, vk VirtualKey) error {
	if vk.CreatedAt.IsZero() {
		vk.CreatedAt = time.Now().UTC()
	}
	vk.Prefix = plain[:min(len(plain), len("sk-atlas-")+len(s.env)+1+8)]
	b, _ := json.Marshal(vk)
	h := hashKey(plain)
	pipe := s.rdb.TxPipeline()
	pipe.Set(ctx, hashPrefix+h, b, 0)
	pipe.HSet(ctx, indexKey, vk.Prefix, h)
	_, err := pipe.Exec(ctx)
	return err
}

// Lookup 은 평문 키를 검증한다. 캐시가 없으므로 폐기는 즉시 반영된다.
func (s *Store) Lookup(ctx context.Context, plain string) (VirtualKey, error) {
	var vk VirtualKey
	if !strings.HasPrefix(plain, "sk-atlas-") {
		return vk, ErrInvalid
	}
	b, err := s.rdb.Get(ctx, hashPrefix+hashKey(plain)).Bytes()
	if errors.Is(err, redis.Nil) {
		return vk, ErrInvalid
	}
	if err != nil {
		return vk, err
	}
	if err := json.Unmarshal(b, &vk); err != nil {
		return vk, err
	}
	if vk.ExpiresAt != nil && time.Now().After(*vk.ExpiresAt) {
		return vk, ErrExpired
	}
	return vk, nil
}

// Revoke 는 prefix 로 키를 폐기한다.
func (s *Store) Revoke(ctx context.Context, prefix string) (bool, error) {
	h, err := s.rdb.HGet(ctx, indexKey, prefix).Result()
	if errors.Is(err, redis.Nil) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	pipe := s.rdb.TxPipeline()
	pipe.Del(ctx, hashPrefix+h)
	pipe.HDel(ctx, indexKey, prefix)
	_, err = pipe.Exec(ctx)
	return err == nil, err
}

func (s *Store) List(ctx context.Context) ([]VirtualKey, error) {
	idx, err := s.rdb.HGetAll(ctx, indexKey).Result()
	if err != nil {
		return nil, err
	}
	out := make([]VirtualKey, 0, len(idx))
	for _, h := range idx {
		b, err := s.rdb.Get(ctx, hashPrefix+h).Bytes()
		if err != nil {
			continue
		}
		var vk VirtualKey
		if json.Unmarshal(b, &vk) == nil {
			out = append(out, vk)
		}
	}
	return out, nil
}
