package econ

import (
	"errors"
	"math"
	"math/big"
	"time"
)

// 비용 시뮬레이터 (FR-EC-CTRL-06): 과업당 토큰·캐시율·배치율·라우팅 비율·모델을 바꿔 과업당/월 비용과
// 레버별 절감 워터폴(캐시 → 배치 → 라우팅 → 모델), 민감도 표를 계산한다. 가격은 at 시점 가격표 고정.
// 추정치이므로 float64 로 계산하고 소수 6자리로 반올림한다(청구 계산은 cost 패키지의 big.Rat).

type SimInput struct {
	TokensPerTask int     `json:"tokens_per_task"`
	InputRatio    float64 `json:"input_ratio"`
	CacheRatio    float64 `json:"cache_ratio"`     // 입력 중 캐시 가능한 prefix 비율
	ReadsPerWrite float64 `json:"reads_per_write"` // 캐시 1회 쓰기당 읽기 횟수
	BatchRatio    float64 `json:"batch_ratio"`
	RouteRatio    float64 `json:"route_ratio"` // 저가 모델로 보낼 과업 비율
	RouteModel    string  `json:"route_model"`
	Turns         *struct {
		N int `json:"n"` // 턴 수
		S int `json:"s"` // 턴당 새 입력 토큰
		D int `json:"d"` // 턴마다 누적되는 컨텍스트 증가분 → 입력 = N·S + d·N(N−1)/2
		O int `json:"o"` // 턴당 출력 토큰
	} `json:"turns,omitempty"`
	TasksPerDay int    `json:"tasks_per_day"`
	Model       string `json:"model"`
	AltModel    string `json:"alt_model"` // 모델 레버: 전체를 이 모델로 바꿨을 때
}

type SimStep struct {
	Lever       string  `json:"lever"`
	PerTaskUSD  float64 `json:"per_task_usd"`
	MonthlyUSD  float64 `json:"monthly_usd"`
	SavingsUSD  float64 `json:"savings_monthly_usd"`
	SavingsPct  float64 `json:"savings_pct"`
	Description string  `json:"description"`
}

type SimOutput struct {
	PriceAsOf      string               `json:"price_as_of"`
	InputTokens    float64              `json:"input_tokens_per_task"`
	OutputTokens   float64              `json:"output_tokens_per_task"`
	Waterfall      []SimStep            `json:"waterfall"`
	FinalPerTask   float64              `json:"final_per_task_usd"`
	FinalMonthly   float64              `json:"final_monthly_usd"`
	TotalSavingPct float64              `json:"total_saving_pct"`
	Sensitivity    map[string][]SensRow `json:"sensitivity"`
}

type SensRow struct {
	Value      float64 `json:"value"`
	MonthlyUSD float64 `json:"monthly_usd"`
}

type PriceFunc func(model string) (in, out, cachedIn, cacheWrite float64, ok bool)

// RatPrices 는 cost.Table.Prices 를 float 단가로 바꾸는 어댑터다.
func RatPrices(f func(model string, at time.Time) (in, out, cr, cw *big.Rat, ok bool), at time.Time) PriceFunc {
	return func(m string) (float64, float64, float64, float64, bool) {
		a, b, c, d, ok := f(m, at)
		fa, _ := a.Float64()
		fb, _ := b.Float64()
		fc, _ := c.Float64()
		fd, _ := d.Float64()
		return fa, fb, fc, fd, ok
	}
}

var ErrSim = errors.New("simulate")

func Simulate(in SimInput, prices PriceFunc, discount float64, at time.Time) (SimOutput, error) {
	if in.TokensPerTask <= 0 && in.Turns == nil {
		in.TokensPerTask = 50000
	}
	if in.InputRatio <= 0 || in.InputRatio > 1 {
		in.InputRatio = 0.8
	}
	if in.ReadsPerWrite <= 0 {
		in.ReadsPerWrite = 5
	}
	if in.TasksPerDay <= 0 {
		in.TasksPerDay = 1000
	}
	for _, r := range []float64{in.CacheRatio, in.BatchRatio, in.RouteRatio} {
		if r < 0 || r > 1 {
			return SimOutput{}, errors.Join(ErrSim, errors.New("비율은 0~1"))
		}
	}
	if _, _, _, _, ok := prices(in.Model); !ok {
		return SimOutput{}, errors.Join(ErrSim, errors.New("가격 없는 모델: "+in.Model))
	}
	inTok, outTok := float64(in.TokensPerTask)*in.InputRatio, float64(in.TokensPerTask)*(1-in.InputRatio)
	if t := in.Turns; t != nil && t.N > 0 {
		inTok = float64(t.N*t.S) + float64(t.D)*float64(t.N*(t.N-1))/2
		outTok = float64(t.N * t.O)
	}
	month := float64(in.TasksPerDay) * 30

	// 과업 하나의 비용: 모델 m, 캐시 비율 c, 배치 비율 b
	task := func(m string, c, b float64) float64 {
		pi, po, pcr, pcw, _ := prices(m)
		cached := inTok * c
		read, write := cached*in.ReadsPerWrite/(in.ReadsPerWrite+1), cached/(in.ReadsPerWrite+1)
		usd := ((inTok-cached)*pi + read*pcr + write*pcw + outTok*po) / 1e6
		return usd * (1 - b*discount)
	}
	full := func(c, b, rr float64, alt string) float64 {
		m := in.Model
		if alt != "" {
			m = alt
		}
		v := task(m, c, b)
		if rr > 0 && in.RouteModel != "" {
			v = (1-rr)*v + rr*task(in.RouteModel, c, b)
		}
		return v
	}
	steps := []struct {
		lever, desc string
		c, b, rr    float64
		alt         string
	}{
		{"baseline", "레버 없음", 0, 0, 0, ""},
		{"cache", "공급사 prompt cache", in.CacheRatio, 0, 0, ""},
		{"batch", "Batch API 이관", in.CacheRatio, in.BatchRatio, 0, ""},
		{"routing", "저가 모델 라우팅 → " + in.RouteModel, in.CacheRatio, in.BatchRatio, in.RouteRatio, ""},
	}
	if in.AltModel != "" {
		if _, _, _, _, ok := prices(in.AltModel); !ok {
			return SimOutput{}, errors.Join(ErrSim, errors.New("가격 없는 모델: "+in.AltModel))
		}
		steps = append(steps, struct {
			lever, desc string
			c, b, rr    float64
			alt         string
		}{"model", "기본 모델 교체 → " + in.AltModel, in.CacheRatio, in.BatchRatio, in.RouteRatio, in.AltModel})
	}
	out := SimOutput{PriceAsOf: at.Format("2006-01-02"), InputTokens: round(inTok), OutputTokens: round(outTok), Sensitivity: map[string][]SensRow{}}
	prev, base := 0.0, 0.0
	for i, s := range steps {
		v := full(s.c, s.b, s.rr, s.alt)
		st := SimStep{Lever: s.lever, PerTaskUSD: round(v), MonthlyUSD: round(v * month), Description: s.desc}
		if i == 0 {
			base = v
		} else {
			st.SavingsUSD = round((prev - v) * month)
			if base > 0 {
				st.SavingsPct = round((prev - v) / base * 100)
			}
		}
		prev = v
		out.Waterfall = append(out.Waterfall, st)
	}
	out.FinalPerTask, out.FinalMonthly = round(prev), round(prev*month)
	if base > 0 {
		out.TotalSavingPct = round((base - prev) / base * 100)
	}
	alt := ""
	if in.AltModel != "" {
		alt = in.AltModel
	}
	for _, d := range []float64{-0.2, -0.1, 0, 0.1, 0.2} {
		cl := func(x float64) float64 { return math.Max(0, math.Min(1, x+d)) }
		out.Sensitivity["cache_ratio"] = append(out.Sensitivity["cache_ratio"], SensRow{round(cl(in.CacheRatio)), round(full(cl(in.CacheRatio), in.BatchRatio, in.RouteRatio, alt) * month)})
		out.Sensitivity["batch_ratio"] = append(out.Sensitivity["batch_ratio"], SensRow{round(cl(in.BatchRatio)), round(full(in.CacheRatio, cl(in.BatchRatio), in.RouteRatio, alt) * month)})
		out.Sensitivity["route_ratio"] = append(out.Sensitivity["route_ratio"], SensRow{round(cl(in.RouteRatio)), round(full(in.CacheRatio, in.BatchRatio, cl(in.RouteRatio), alt) * month)})
	}
	return out, nil
}

func round(v float64) float64 { return math.Round(v*1e6) / 1e6 }
