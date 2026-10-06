// Package ratelimit 는 Redis 원자 연산 기반 RPM 토큰 버킷 · TPM 예약/정산이다 (FR-GW-RATE-01·02).
package ratelimit

import (
	"context"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

// 토큰 버킷: capacity=rpm, refill=rpm/60000 per ms.
var bucket = redis.NewScript(`
local cap = tonumber(ARGV[1]); local rate = tonumber(ARGV[2]); local now = tonumber(ARGV[3]); local cost = tonumber(ARGV[4])
local b = redis.call('HMGET', KEYS[1], 't', 'ts')
local tokens = tonumber(b[1]) or cap
local ts = tonumber(b[2]) or now
tokens = math.min(cap, tokens + math.max(0, now - ts) * rate)
local allowed = 0
if tokens >= cost then tokens = tokens - cost; allowed = 1 end
redis.call('HSET', KEYS[1], 't', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(cap / rate) + 1000)
local retry = 0
if allowed == 0 then retry = math.ceil((cost - tokens) / rate) end
return {allowed, math.floor(tokens), retry}
`)

// 고정 윈도(분) TPM 예약: 한도 초과면 되돌리고 거부.
var reserve = redis.NewScript(`
local used = redis.call('INCRBY', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], 120)
if used > tonumber(ARGV[2]) then redis.call('DECRBY', KEYS[1], ARGV[1]); return {0, tonumber(ARGV[2]) - used + tonumber(ARGV[1])} end
return {1, tonumber(ARGV[2]) - used}
`)

type Limiter struct{ rdb *redis.Client }

func New(rdb *redis.Client) *Limiter { return &Limiter{rdb} }

type Decision struct {
	Allowed    bool
	Remaining  int
	RetryAfter time.Duration
}

// AllowRequest 는 scope(예: 키 prefix)의 RPM 버킷에서 1 토큰을 쓴다. rpm<=0 이면 무제한.
func (l *Limiter) AllowRequest(ctx context.Context, scope string, rpm int, now time.Time) (Decision, error) {
	if rpm <= 0 {
		return Decision{Allowed: true, Remaining: -1}, nil
	}
	res, err := bucket.Run(ctx, l.rdb, []string{"rl:" + scope + ":rpm"}, rpm, float64(rpm)/60000, now.UnixMilli(), 1).Int64Slice()
	if err != nil {
		return Decision{}, err
	}
	return Decision{Allowed: res[0] == 1, Remaining: int(res[1]), RetryAfter: time.Duration(res[2]) * time.Millisecond}, nil
}

func tpmKey(scope string, now time.Time) string {
	return fmt.Sprintf("rl:%s:tpm:%d", scope, now.Unix()/60)
}

// ReserveTokens 는 이번 분 TPM 에서 est 토큰을 예약한다. tpm<=0 이면 무제한.
func (l *Limiter) ReserveTokens(ctx context.Context, scope string, tpm, est int, now time.Time) (Decision, error) {
	if tpm <= 0 {
		return Decision{Allowed: true, Remaining: -1}, nil
	}
	res, err := reserve.Run(ctx, l.rdb, []string{tpmKey(scope, now)}, est, tpm).Int64Slice()
	if err != nil {
		return Decision{}, err
	}
	d := Decision{Allowed: res[0] == 1, Remaining: int(res[1])}
	if !d.Allowed {
		d.RetryAfter = time.Duration(60-now.Unix()%60) * time.Second
	}
	return d, nil
}

// Settle 은 실사용과 예약의 차이를 정산한다(음수면 반환).
func (l *Limiter) Settle(ctx context.Context, scope string, tpm, reserved, actual int, reservedAt time.Time) error {
	if tpm <= 0 || actual == reserved {
		return nil
	}
	return l.rdb.IncrBy(ctx, tpmKey(scope, reservedAt), int64(actual-reserved)).Err()
}
