package econ

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math/big"
	"time"

	"github.com/redis/go-redis/v9"

	"atlas-gateway/internal/provider"
)

// ---- 공급사 prompt cache 지시 (FR-EC-CACHE-01) ----

// PromptCacheKey 는 hash(tenant, alias, system_prompt_id) 다. prefix 가 min 토큰 미만이면 "" (지시 생략).
func PromptCacheKey(tenant, alias string, msgs []provider.Message, tools int, minTokens int) string {
	last := len(msgs) - 1
	for last >= 0 && msgs[last].Role != "user" {
		last--
	}
	if last <= 0 {
		return ""
	}
	if provider.EstimateTokens(provider.PromptText(msgs[:last]))+tools*64 < minTokens {
		return ""
	}
	sys := ""
	for _, m := range msgs {
		if m.Role == "system" {
			sys += m.Content
		}
	}
	h := sha256.Sum256([]byte(tenant + "\x00" + alias + "\x00" + sys))
	return "atlas-" + hex.EncodeToString(h[:8])
}

// CacheTTL 은 같은 키의 직전 사용 간격이 5분을 넘으면 1h(쓰기 2x, 2회 읽기부터 이득), 아니면 5m.
func CacheTTL(ctx context.Context, r *redis.Client, key string, now time.Time) string {
	k := "gw:pc:last:" + key
	prev, err := r.GetSet(ctx, k, now.Unix()).Int64()
	r.Expire(ctx, k, 2*time.Hour)
	if err == nil && now.Unix()-prev > 300 {
		return "1h"
	}
	return "5m"
}

// ---- exact 응답 캐시 (FR-EC-CACHE-04) ----

type Cached struct {
	Content      string              `json:"content"`
	ToolCalls    []provider.ToolCall `json:"tool_calls,omitempty"`
	FinishReason string              `json:"finish_reason"`
	Prompt       int                 `json:"prompt_tokens"`
	Completion   int                 `json:"completion_tokens"`
	Alias        string              `json:"alias"`
	Provider     string              `json:"provider"`
	Model        string              `json:"model"`
	CostE8       int64               `json:"cost_e8"`
	At           time.Time           `json:"at"`
}

// ExactKey 는 SHA-256(canonical JSON{tenant, alias, messages, tools, temperature, top_p, seed, response_format}).
// encoding/json 은 map 키를 정렬하므로 같은 요청은 같은 바이트가 된다.
// effort 는 최종 계획 effort(헤더·본문·계산 정책 반영) — 추론 깊이가 다르면 다른 응답이다.
func ExactKey(tenant, alias, effort string, msgs []provider.Message, generic map[string]any) string {
	doc := map[string]any{"tenant": tenant, "alias": alias, "messages": msgs, "effort": effort}
	for _, k := range []string{"tools", "tool_choice", "temperature", "top_p", "seed", "response_format", "max_tokens"} {
		if v, ok := generic[k]; ok {
			doc[k] = v
		}
	}
	b, _ := json.Marshal(doc)
	h := sha256.Sum256(b)
	return "gw:xc:" + hex.EncodeToString(h[:])
}

// ExactEligible: temperature 가 명시적으로 0 일 때만 (temperature>0·미지정은 기본 비활성).
func ExactEligible(enabled bool, generic map[string]any, cacheControl string) (bool, string) {
	if !enabled {
		return false, "DISABLED"
	}
	if cacheControl == "no-cache" || cacheControl == "no-store" {
		return false, "BYPASS"
	}
	t, ok := generic["temperature"].(float64)
	if !ok || t != 0 {
		return false, "SKIP-TEMPERATURE"
	}
	return true, ""
}

func GetExact(ctx context.Context, r *redis.Client, key string) (*Cached, error) {
	raw, err := r.Get(ctx, key).Bytes()
	if errors.Is(err, redis.Nil) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var c Cached
	if err := json.Unmarshal(raw, &c); err != nil {
		return nil, nil
	}
	return &c, nil
}

func PutExact(ctx context.Context, r *redis.Client, key string, c Cached, ttl time.Duration) {
	b, _ := json.Marshal(c)
	r.Set(ctx, key, b, ttl)
}

// ---- 캐시 적중·절감 KPI (FR-EC-CACHE-03) ----

func kpiKey(tenant, alias string) string { return "gw:econ:cache:" + tenant + ":" + alias }

// RecordPrompt 는 공급사 prompt cache 사용량을 누적한다. savings 는 1e-8 USD (쓰기 할증이면 음수).
func RecordPrompt(ctx context.Context, r *redis.Client, tenant, alias string, u provider.Usage, savingsE8 int64, ttl string) {
	k := kpiKey(tenant, alias)
	p := r.TxPipeline()
	p.HIncrBy(ctx, k, "requests", 1)
	p.HIncrBy(ctx, k, "input", int64(u.Prompt))
	p.HIncrBy(ctx, k, "cached_read", int64(u.Cached))
	p.HIncrBy(ctx, k, "cache_write", int64(u.CacheWrite))
	if u.CacheWrite > 0 {
		p.HIncrBy(ctx, k, "writes_"+ttl, 1)
	}
	if u.Cached > 0 {
		p.HIncrBy(ctx, k, "reads", 1)
	}
	p.HIncrBy(ctx, k, "savings_e8", savingsE8)
	p.SAdd(ctx, "gw:econ:cache:keys", tenant+"\x00"+alias)
	_, _ = p.Exec(ctx)
}

func RecordExactHit(ctx context.Context, r *redis.Client, tenant, alias string, savedE8 int64) {
	k := kpiKey(tenant, alias)
	p := r.TxPipeline()
	p.HIncrBy(ctx, k, "exact_hits", 1)
	p.HIncrBy(ctx, k, "exact_saved_e8", savedE8)
	p.SAdd(ctx, "gw:econ:cache:keys", tenant+"\x00"+alias)
	_, _ = p.Exec(ctx)
}

type CacheKPI struct {
	Tenant          string  `json:"tenant"`
	Alias           string  `json:"alias"`
	Requests        int64   `json:"requests"`
	InputTokens     int64   `json:"input_tokens"`
	CachedRead      int64   `json:"cached_read_tokens"`
	CacheWrite      int64   `json:"cache_write_tokens"`
	HitRatio        float64 `json:"hit_ratio"`
	PromptSavings   string  `json:"prompt_cache_savings_usd"`
	ExactHits       int64   `json:"exact_hits"`
	ExactSavings    string  `json:"exact_cache_savings_usd"`
	ReadWriteRatio  float64 `json:"read_write_ratio"`
	Warning         string  `json:"warning,omitempty"`
	TotalSavingsUSD string  `json:"total_savings_usd"`
}

// KPI: hit_ratio = cached_read / input(전체). 쓰기 대비 읽기 < 1(5m) · < 2(1h) 면 '손해 캐시' 경고.
func KPI(ctx context.Context, r *redis.Client, tenant string) ([]CacheKPI, error) {
	members, err := r.SMembers(ctx, "gw:econ:cache:keys").Result()
	if err != nil {
		return nil, err
	}
	out := []CacheKPI{}
	for _, m := range members {
		t, a := splitNul(m)
		if tenant != "" && t != tenant {
			continue
		}
		h, err := r.HGetAll(ctx, kpiKey(t, a)).Result()
		if err != nil {
			return nil, err
		}
		n := func(f string) int64 { v, _ := json.Number(h[f]).Int64(); return v }
		k := CacheKPI{Tenant: t, Alias: a, Requests: n("requests"), InputTokens: n("input"), CachedRead: n("cached_read"),
			CacheWrite: n("cache_write"), ExactHits: n("exact_hits")}
		if k.InputTokens > 0 {
			k.HitRatio = float64(k.CachedRead) / float64(k.InputTokens)
		}
		writes, reads := n("writes_5m")+n("writes_1h"), n("reads")
		if writes > 0 {
			k.ReadWriteRatio = float64(reads) / float64(writes)
			need := 1.0
			if n("writes_1h") > n("writes_5m") {
				need = 2
			}
			if k.ReadWriteRatio < need {
				k.Warning = "손해 캐시: 쓰기 대비 읽기가 부족 — prefix 안정성·TTL 점검"
			}
		}
		ps, es := big.NewRat(n("savings_e8"), 1e8), big.NewRat(n("exact_saved_e8"), 1e8)
		k.PromptSavings, k.ExactSavings = ps.FloatString(8), es.FloatString(8)
		k.TotalSavingsUSD = new(big.Rat).Add(ps, es).FloatString(8)
		out = append(out, k)
	}
	return out, nil
}

func splitNul(s string) (string, string) {
	for i := 0; i < len(s); i++ {
		if s[i] == 0 {
			return s[:i], s[i+1:]
		}
	}
	return s, ""
}

// PurgeExact 는 gw:xc:* 를 모두 지운다.
func PurgeExact(ctx context.Context, r *redis.Client) (int64, error) {
	var n int64
	iter := r.Scan(ctx, 0, "gw:xc:*", 500).Iterator()
	for iter.Next(ctx) {
		if d, err := r.Del(ctx, iter.Val()).Result(); err == nil {
			n += d
		}
	}
	return n, iter.Err()
}
