package econ

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"sort"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

// Budget 은 계층 예산이다 (FR-EC-CTRL-01). scope: org(=가상키 tenant) · project(프로젝트 id 또는 org/proj) · env(org/proj/env, 가상키 metadata.env) · key(가상키 prefix).
// Manager 예산 상속(FR-PLT-TEN-04)은 org/proj · org/proj/env 형식으로 내려 보낸다.
type Budget struct {
	ID             string `json:"id"`
	Scope          string `json:"scope"`
	ScopeID        string `json:"scope_id"`
	AmountUSD      string `json:"amount_usd"`
	Period         string `json:"period"` // daily | weekly | monthly
	SoftPct        int    `json:"soft_pct"`
	HardAction     string `json:"hard_action"` // block | downgrade | alert
	DowngradeAlias string `json:"downgrade_alias,omitempty"`
	TZ             string `json:"tz"`
}

type BudgetStatus struct {
	Budget
	WindowStart string `json:"window_start"`
	SpentUSD    string `json:"spent_usd"`
	Pct         int    `json:"pct"`
	State       string `json:"state"` // ok | warning | exceeded
}

// Scopes 는 요청 하나가 속한 예산 범위다.
type Scopes struct{ Org, Project, Env, Key string }

func (s Scopes) match(b Budget) bool {
	switch b.Scope {
	case "org":
		return b.ScopeID == s.Org
	case "project":
		return s.Project != "" && (b.ScopeID == s.Project || b.ScopeID == s.Org+"/"+s.Project)
	case "env":
		return s.Env != "" && b.ScopeID == s.Org+"/"+s.Project+"/"+s.Env
	case "key":
		return b.ScopeID == s.Key
	}
	return false
}

type Budgets struct {
	R       *redis.Client
	Now     func() time.Time
	SoftPct int
	Publish func(ctx context.Context, subject string, data any)
}

const budgetsKey = "gw:budgets"

var ErrBadBudget = errors.New("invalid budget")

func (b *Budgets) Validate(x *Budget, aliasKnown func(string) bool) error {
	if x.ID == "" || x.ScopeID == "" {
		return fmt.Errorf("%w: id·scope_id 필수", ErrBadBudget)
	}
	switch x.Scope {
	case "org", "project", "env", "key":
	default:
		return fmt.Errorf("%w: scope = org | project | env | key", ErrBadBudget)
	}
	switch x.Period {
	case "daily", "weekly", "monthly":
	default:
		return fmt.Errorf("%w: period = daily | weekly | monthly", ErrBadBudget)
	}
	if amt, ok := new(big.Rat).SetString(x.AmountUSD); !ok || amt.Sign() <= 0 {
		return fmt.Errorf("%w: amount_usd 는 양수 decimal", ErrBadBudget)
	}
	if x.HardAction == "" {
		x.HardAction = "block"
	}
	switch x.HardAction {
	case "block", "alert":
	case "downgrade":
		if !aliasKnown(x.DowngradeAlias) {
			return fmt.Errorf("%w: downgrade 는 알려진 downgrade_alias 필요", ErrBadBudget)
		}
	default:
		return fmt.Errorf("%w: hard_action = block | downgrade | alert", ErrBadBudget)
	}
	if x.SoftPct <= 0 || x.SoftPct >= 100 {
		x.SoftPct = b.SoftPct
	}
	if x.TZ == "" {
		x.TZ = "UTC"
	}
	if _, err := time.LoadLocation(x.TZ); err != nil {
		return fmt.Errorf("%w: tz %s", ErrBadBudget, x.TZ)
	}
	return nil
}

// Put 은 예산을 저장하고, 현재 기간의 실제 지출(범위별 시간 버킷 합)로 사용액을 시드한다 —
// 기간 중간에 만든 예산도 그 기간에 이미 쓴 금액을 반영한다.
func (b *Budgets) Put(ctx context.Context, x Budget) error {
	raw, _ := json.Marshal(x)
	if err := b.R.HSet(ctx, budgetsKey, x.ID, raw).Err(); err != nil {
		return err
	}
	now := b.Now()
	w := window(x, now)
	loc, err := time.LoadLocation(x.TZ)
	if err != nil {
		loc = time.UTC
	}
	start, _ := time.ParseInLocation("2006-01-02", w, loc)
	var keys []string
	for h := start.UTC().Truncate(time.Hour); !h.After(now); h = h.Add(time.Hour) {
		keys = append(keys, hourKey(x.Scope, x.ScopeID, h))
	}
	var sum int64
	for i := 0; i < len(keys); i += 256 {
		vals, err := b.R.MGet(ctx, keys[i:min(len(keys), i+256)]...).Result()
		if err != nil {
			return err
		}
		for _, v := range vals {
			if s, ok := v.(string); ok {
				n, _ := strconv.ParseInt(s, 10, 64)
				sum += n
			}
		}
	}
	return b.R.Set(ctx, spentKey(x, w), sum, 40*24*time.Hour).Err()
}

// hourKey 는 범위별 시간(UTC) 지출 버킷이다 (예산 유무와 무관하게 항상 누적, 35일 보관).
func hourKey(scope, id string, t time.Time) string {
	return "gw:spend:h:" + scope + ":" + id + ":" + t.UTC().Format("2006010215")
}

func (b *Budgets) Delete(ctx context.Context, id string) (bool, error) {
	n, err := b.R.HDel(ctx, budgetsKey, id).Result()
	return n > 0, err
}

func (b *Budgets) all(ctx context.Context) ([]Budget, error) {
	m, err := b.R.HGetAll(ctx, budgetsKey).Result()
	if err != nil {
		return nil, err
	}
	out := make([]Budget, 0, len(m))
	for _, v := range m {
		var x Budget
		if json.Unmarshal([]byte(v), &x) == nil {
			out = append(out, x)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out, nil
}

// window 은 예산 기간 시작일(예산 tz 기준)이다.
func window(x Budget, now time.Time) string {
	loc, err := time.LoadLocation(x.TZ)
	if err != nil {
		loc = time.UTC
	}
	t := now.In(loc)
	d := time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, loc)
	switch x.Period {
	case "weekly":
		d = d.AddDate(0, 0, -((int(d.Weekday()) + 6) % 7)) // 월요일 시작
	case "monthly":
		d = time.Date(t.Year(), t.Month(), 1, 0, 0, 0, 0, loc)
	}
	return d.Format("2006-01-02")
}

func spentKey(x Budget, w string) string { return "gw:budget:spent:" + x.ID + ":" + w }

func (b *Budgets) status(ctx context.Context, x Budget) (BudgetStatus, error) {
	w := window(x, b.Now())
	e8, err := b.R.Get(ctx, spentKey(x, w)).Int64()
	if err != nil && !errors.Is(err, redis.Nil) {
		return BudgetStatus{}, err
	}
	amt, _ := new(big.Rat).SetString(x.AmountUSD)
	spent := big.NewRat(e8, 100_000_000)
	pct := 0
	if amt.Sign() > 0 {
		f, _ := new(big.Rat).Quo(new(big.Rat).Mul(spent, big.NewRat(100, 1)), amt).Float64()
		pct = int(f)
	}
	st := "ok"
	switch {
	case pct >= 100:
		st = "exceeded"
	case pct >= x.SoftPct:
		st = "warning"
	}
	return BudgetStatus{Budget: x, WindowStart: w, SpentUSD: spent.FloatString(8), Pct: pct, State: st}, nil
}

func (b *Budgets) List(ctx context.Context) ([]BudgetStatus, error) {
	all, err := b.all(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]BudgetStatus, 0, len(all))
	for _, x := range all {
		s, err := b.status(ctx, x)
		if err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, nil
}

// Verdict 는 예산 판정이다. Action: allow | block | downgrade.
type Verdict struct {
	Action   string
	Alias    string // downgrade 대상
	Budget   string // 차단·하향을 일으킨 예산
	Warnings []string
}

// Check 는 요청 전 예산을 본다: 초과 예산의 hard_action 중 block > downgrade > alert 순으로 강한 것을 적용.
func (b *Budgets) Check(ctx context.Context, sc Scopes) (Verdict, error) {
	all, err := b.all(ctx)
	if err != nil {
		return Verdict{}, err
	}
	v := Verdict{Action: "allow"}
	for _, x := range all {
		if !sc.match(x) {
			continue
		}
		s, err := b.status(ctx, x)
		if err != nil {
			return v, err
		}
		switch s.State {
		case "warning":
			v.Warnings = append(v.Warnings, fmt.Sprintf("%s:%s %d%%", x.Scope, x.ScopeID, s.Pct))
		case "exceeded":
			v.Warnings = append(v.Warnings, fmt.Sprintf("%s:%s %d%% exceeded", x.Scope, x.ScopeID, s.Pct))
			switch {
			case x.HardAction == "block":
				v.Action, v.Budget = "block", x.ID
			case x.HardAction == "downgrade" && v.Action == "allow":
				v.Action, v.Alias, v.Budget = "downgrade", x.DowngradeAlias, x.ID
			}
		}
	}
	return v, nil
}

// Settle 은 요청 비용(1e-8 USD)을 해당 예산들에 더하고 상태가 바뀌면 budget.state_changed 를 낸다.
func (b *Budgets) Settle(ctx context.Context, sc Scopes, e8 int64) {
	if e8 <= 0 {
		return
	}
	hp := b.R.TxPipeline()
	buckets := [][2]string{{"org", sc.Org}, {"project", sc.Project}, {"key", sc.Key}}
	if sc.Project != "" {
		buckets = append(buckets, [2]string{"project", sc.Org + "/" + sc.Project})
		if sc.Env != "" {
			buckets = append(buckets, [2]string{"env", sc.Org + "/" + sc.Project + "/" + sc.Env})
		}
	}
	for _, s := range buckets {
		if s[1] != "" {
			k := hourKey(s[0], s[1], b.Now())
			hp.IncrBy(ctx, k, e8)
			hp.Expire(ctx, k, 35*24*time.Hour)
		}
	}
	_, _ = hp.Exec(ctx)
	all, err := b.all(ctx)
	if err != nil {
		return
	}
	for _, x := range all {
		if !sc.match(x) {
			continue
		}
		w := window(x, b.Now())
		k := spentKey(x, w)
		before, _ := b.status(ctx, x)
		pipe := b.R.TxPipeline()
		pipe.IncrBy(ctx, k, e8)
		pipe.Expire(ctx, k, 40*24*time.Hour)
		if _, err := pipe.Exec(ctx); err != nil {
			continue
		}
		s, err := b.status(ctx, x)
		if err != nil {
			continue
		}
		sk := "gw:budget:state:" + x.ID + ":" + w
		prev, _ := b.R.GetSet(ctx, sk, s.State).Result()
		b.R.Expire(ctx, sk, 40*24*time.Hour)
		if prev == "" {
			prev = "ok"
		}
		// 예산 50·80·100% 도달 알림 이벤트 (FR-X-NTF-03) — 이번 정산으로 넘은 임계만
		for _, th := range []int{50, 80, 100} {
			if before.Pct < th && s.Pct >= th && b.Publish != nil {
				b.Publish(ctx, "gw.budget.threshold", map[string]any{"budget_id": x.ID, "org": sc.Org, "scope": x.Scope, "scope_id": x.ScopeID,
					"threshold": th, "pct": s.Pct, "spent_usd": s.SpentUSD, "amount_usd": x.AmountUSD, "window_start": w, "hard_action": x.HardAction})
			}
		}
		if prev != s.State && b.Publish != nil {
			b.Publish(ctx, "gw.budget.state_changed", map[string]any{"budget_id": x.ID, "org": sc.Org, "scope": x.Scope, "scope_id": x.ScopeID,
				"from": prev, "to": s.State, "pct": s.Pct, "spent_usd": s.SpentUSD, "amount_usd": x.AmountUSD, "window_start": w,
				"hard_action": x.HardAction})
		}
	}
}

// ---- 과업 토큰 예산 (FR-EC-CTRL-03) ----

type Tasks struct {
	R       *redis.Client
	Default int
	SoftPct int
	Publish func(ctx context.Context, subject string, data any)
}

func taskKey(id string) string { return "gw:task:" + id }

// Limit 은 요청 헤더(x-atlas-task-budget)가 있으면 그 값(기본보다 작을 때만), 아니면 기본값.
func (t *Tasks) Limit(header string) int {
	if n, err := strconv.Atoi(header); err == nil && n > 0 && (t.Default == 0 || n < t.Default) {
		return n
	}
	return t.Default
}

func (t *Tasks) Used(ctx context.Context, id string) int {
	n, _ := t.R.Get(ctx, taskKey(id)).Int()
	return n
}

// Add 는 과업 누적 토큰을 더하고, soft% 를 처음 넘으면 budget.task_near_limit 을 낸다.
func (t *Tasks) Add(ctx context.Context, id string, tokens, limit int, tenant string) int {
	if tokens <= 0 {
		return t.Used(ctx, id)
	}
	n, err := t.R.IncrBy(ctx, taskKey(id), int64(tokens)).Result()
	if err != nil {
		return 0
	}
	t.R.Expire(ctx, taskKey(id), 24*time.Hour)
	if limit > 0 && int(n)*100 >= limit*t.SoftPct && t.Publish != nil {
		if ok, _ := t.R.SetNX(ctx, taskKey(id)+":warned", 1, 24*time.Hour).Result(); ok {
			t.Publish(ctx, "gw.budget.task_near_limit", map[string]any{"task_id": id, "tenant": tenant, "used": n, "limit": limit})
		}
	}
	return int(n)
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
