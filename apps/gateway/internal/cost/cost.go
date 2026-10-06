// Package cost 는 버전 가격표와 정밀 비용 계산이다 (FR-GW-COST-02·03). 부동소수 금지 — big.Rat, 소수 8자리.
package cost

import (
	"fmt"
	"math/big"
	"sort"
	"time"

	"atlas-gateway/internal/config"
)

type Table struct {
	versions map[string][]version
}

type version struct {
	from                             time.Time
	input, output, cachedIn, cacheWr *big.Rat
}

func NewTable(prices map[string][]config.PriceVersion) (*Table, error) {
	t := &Table{versions: map[string][]version{}}
	for model, vs := range prices {
		for _, v := range vs {
			from, err := time.Parse("2006-01-02", v.EffectiveFrom)
			if err != nil {
				return nil, fmt.Errorf("price %s: %w", model, err)
			}
			in, ok1 := new(big.Rat).SetString(v.Input)
			out, ok2 := new(big.Rat).SetString(v.Output)
			if !ok1 || !ok2 {
				return nil, fmt.Errorf("price %s: decimal 형식 오류", model)
			}
			cr, cw := in, in
			if v.CachedInput != "" {
				if cr, ok1 = new(big.Rat).SetString(v.CachedInput); !ok1 {
					return nil, fmt.Errorf("price %s: cached_input 형식 오류", model)
				}
			}
			if v.CacheWrite != "" {
				if cw, ok1 = new(big.Rat).SetString(v.CacheWrite); !ok1 {
					return nil, fmt.Errorf("price %s: cache_write 형식 오류", model)
				}
			}
			t.versions[model] = append(t.versions[model], version{from, in, out, cr, cw})
		}
		sort.Slice(t.versions[model], func(i, j int) bool { return t.versions[model][i].from.Before(t.versions[model][j].from) })
	}
	return t, nil
}

func (t *Table) at(model string, at time.Time) *version {
	var cur *version
	for i := range t.versions[model] {
		if !t.versions[model][i].from.After(at) {
			cur = &t.versions[model][i]
		}
	}
	return cur
}

// Prices 는 at 시점 단가(USD/1M) — 입력·출력·캐시 읽기·캐시 쓰기.
func (t *Table) Prices(model string, at time.Time) (in, out, cachedIn, cacheWrite *big.Rat, known bool) {
	v := t.at(model, at)
	if v == nil {
		z := new(big.Rat)
		return z, z, z, z, false
	}
	return v.input, v.output, v.cachedIn, v.cacheWr, true
}

// Breakdown 은 캐시를 반영한 비용이다. input 은 공급사 prompt_tokens(캐시 읽기·쓰기 포함) 전체.
// Savings = 같은 토큰을 전부 일반 입력 단가로 냈을 때 대비 절감액 (쓰기 할증이면 음수) — FR-EC-CACHE-03.
type Breakdown struct {
	USD, Savings *big.Rat
	Known        bool
}

func (t *Table) CostCached(model string, input, cached, written, output int, at time.Time) Breakdown {
	v := t.at(model, at)
	if v == nil {
		return Breakdown{new(big.Rat), new(big.Rat), false}
	}
	plain := max(0, input-cached-written)
	m := func(n int, p *big.Rat) *big.Rat { return new(big.Rat).Mul(big.NewRat(int64(n), 1), p) }
	sum := new(big.Rat).Add(m(plain, v.input), m(cached, v.cachedIn))
	sum.Add(sum, m(written, v.cacheWr))
	sum.Add(sum, m(output, v.output))
	mil := big.NewRat(1_000_000, 1)
	sav := new(big.Rat).Sub(m(cached, new(big.Rat).Sub(v.input, v.cachedIn)), m(written, new(big.Rat).Sub(v.cacheWr, v.input)))
	return Breakdown{new(big.Rat).Quo(sum, mil), new(big.Rat).Quo(sav, mil), true}
}

// Cost 는 at 시점 가격으로 (input×p_in + output×p_out) / 1e6 USD 를 계산한다. 가격이 없으면 known=false.
func (t *Table) Cost(model string, input, output int, at time.Time) (usd *big.Rat, known bool) {
	cur := t.at(model, at)
	if cur == nil {
		return new(big.Rat), false
	}
	in := new(big.Rat).Mul(big.NewRat(int64(input), 1), cur.input)
	out := new(big.Rat).Mul(big.NewRat(int64(output), 1), cur.output)
	return new(big.Rat).Quo(new(big.Rat).Add(in, out), big.NewRat(1_000_000, 1)), true
}

// Format 은 소수 8자리 문자열이다 (Decimal(18,8)).
func Format(usd *big.Rat) string { return usd.FloatString(8) }

// E8 은 1e-8 USD 정수 단위다(Redis 누적용, 반올림).
func E8(usd *big.Rat) int64 {
	v := new(big.Rat).Mul(usd, big.NewRat(100_000_000, 1))
	n := new(big.Int).Quo(new(big.Int).Add(v.Num(), new(big.Int).Quo(v.Denom(), big.NewInt(2))), v.Denom())
	return n.Int64()
}
