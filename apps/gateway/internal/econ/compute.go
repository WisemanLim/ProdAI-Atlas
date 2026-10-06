package econ

// 계산 정책 (FR-BL-COMP-01·02·05): 과업·에이전트·테넌트별 reasoning effort·thinking 예산·max output 을 정책으로 정하고,
// effort "auto" 는 난이도 추정으로 고르며, 정책별 추론 토큰 예산이 80% 를 넘으면 effort 를 한 단계 낮춘다.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"

	"atlas-gateway/internal/config"
)

const keyCompute = "atlas:gw:compute"

type ComputeMatch struct {
	Agent    string `json:"agent,omitempty"`     // x-atlas-tags unit_id 또는 agent_id
	TaskType string `json:"task_type,omitempty"` // x-atlas-task-type
	Tenant   string `json:"tenant,omitempty"`
	Alias    string `json:"alias,omitempty"`
}

type ComputePolicy struct {
	ID              string       `json:"id"`
	Match           ComputeMatch `json:"match"`
	Effort          string       `json:"effort"` // minimal | low | medium | high | auto
	ThinkingBudget  int          `json:"thinking_budget_tokens,omitempty"`
	MaxOutput       int          `json:"max_output_tokens,omitempty"`
	ReasoningBudget int          `json:"reasoning_budget_tokens,omitempty"` // 하루 추론 토큰 예산 (0 = 없음)
	DowngradePct    int          `json:"downgrade_pct,omitempty"`           // 기본 80
	UpdatedAt       time.Time    `json:"updated_at"`
}

func (p ComputePolicy) specificity() int {
	n := 0
	for _, v := range []string{p.Match.Agent, p.Match.TaskType, p.Match.Tenant, p.Match.Alias} {
		if v != "" {
			n++
		}
	}
	return n
}

type Compute struct {
	R       *redis.Client
	Now     func() time.Time
	Publish func(ctx context.Context, subject string, data any)
}

var ErrBadPolicy = errors.New("invalid compute policy")

var effortLevels = []string{"minimal", "low", "medium", "high"}

func (c *Compute) Put(ctx context.Context, p ComputePolicy) (ComputePolicy, error) {
	if p.ID == "" {
		return p, fmt.Errorf("%w: id 필수", ErrBadPolicy)
	}
	if _, ok := config.Efforts[p.Effort]; !ok && p.Effort != "auto" {
		return p, fmt.Errorf("%w: effort = minimal|low|medium|high|auto", ErrBadPolicy)
	}
	if p.ThinkingBudget < 0 || p.MaxOutput < 0 || p.ReasoningBudget < 0 || p.DowngradePct < 0 || p.DowngradePct > 100 {
		return p, fmt.Errorf("%w: 음수 불가", ErrBadPolicy)
	}
	p.UpdatedAt = c.Now().UTC()
	b, _ := json.Marshal(p)
	return p, c.R.HSet(ctx, keyCompute, p.ID, b).Err()
}

func (c *Compute) List(ctx context.Context) []ComputePolicy {
	raw, _ := c.R.HGetAll(ctx, keyCompute).Result()
	out := make([]ComputePolicy, 0, len(raw))
	for _, v := range raw {
		var p ComputePolicy
		if json.Unmarshal([]byte(v), &p) == nil {
			out = append(out, p)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func (c *Compute) Delete(ctx context.Context, id string) error {
	return c.R.HDel(ctx, keyCompute, id).Err()
}

// Difficulty 는 경량 난이도 추정이다 (BL-COMP-02): 입력 길이·도구 수·과제 표현 → easy | medium | hard.
func Difficulty(text string, inputTokens, tools int) (string, int) {
	score := 0
	switch {
	case inputTokens > 3000:
		score += 2
	case inputTokens > 800:
		score++
	}
	if tools > 3 {
		score++
	}
	t := strings.ToLower(text)
	hard := 0
	for _, w := range []string{"증명", "단계별", "최적화", "알고리즘", "분석해", "원인", "왜 ", "prove", "step by step", "optimi", "algorithm", "derive", "debug"} {
		if strings.Contains(t, w) {
			hard++
		}
	}
	score += min(hard, 3)
	for _, w := range []string{"번역", "요약", "분류", "translate", "summarize", "classify", "yes or no"} {
		if strings.Contains(t, w) {
			score--
			break
		}
	}
	switch {
	case score >= 3:
		return "hard", score
	case score >= 1:
		return "medium", score
	}
	return "easy", score
}

var difficultyEffort = map[string]string{"easy": "low", "medium": "medium", "hard": "high"}

type ComputeDecision struct {
	Policy     string
	Effort     string
	Thinking   int
	MaxOutput  int
	Difficulty string
	Downgraded string // "high->medium"
}

func usageKey(id string, day time.Time) string {
	return keyCompute + ":usage:" + id + ":" + day.UTC().Format("20060102")
}

// Decide 는 가장 구체적인 정책을 골라 effort 등을 정한다. 명시 effort 가 있으면 정책 effort 대신 그것(상한은 정책 effort).
func (c *Compute) Decide(ctx context.Context, m ComputeMatch, explicit, text string, inputTokens, tools int) (ComputeDecision, bool) {
	var best *ComputePolicy
	for _, p := range c.List(ctx) {
		if (p.Match.Agent != "" && p.Match.Agent != m.Agent) || (p.Match.TaskType != "" && p.Match.TaskType != m.TaskType) ||
			(p.Match.Tenant != "" && p.Match.Tenant != m.Tenant) || (p.Match.Alias != "" && p.Match.Alias != m.Alias) {
			continue
		}
		if best == nil || p.specificity() > best.specificity() {
			pp := p
			best = &pp
		}
	}
	if best == nil {
		return ComputeDecision{}, false
	}
	d := ComputeDecision{Policy: best.ID, Thinking: best.ThinkingBudget, MaxOutput: best.MaxOutput, Effort: best.Effort}
	if d.Effort == "auto" {
		d.Difficulty, _ = Difficulty(text, inputTokens, tools)
		d.Effort = difficultyEffort[d.Difficulty]
	}
	if explicit != "" && config.Efforts[explicit] < config.Efforts[d.Effort] {
		d.Effort = explicit // 호출자가 더 낮게 요청하면 존중 (정책은 상한)
	}
	if best.ReasoningBudget > 0 {
		used, _ := c.R.Get(ctx, usageKey(best.ID, c.Now())).Int()
		pct := best.DowngradePct
		if pct == 0 {
			pct = 80
		}
		if used*100 >= best.ReasoningBudget*pct {
			if i := indexOf(effortLevels, d.Effort); i > 0 {
				from := d.Effort
				d.Effort = effortLevels[i-1]
				d.Downgraded = from + "->" + d.Effort
				if d.Thinking > 0 {
					d.Thinking /= 2
				}
				flag := usageKey(best.ID, c.Now()) + ":notified"
				if ok, _ := c.R.SetNX(ctx, flag, 1, 26*time.Hour).Result(); ok && c.Publish != nil {
					c.Publish(ctx, "gw.compute.downgraded", map[string]any{"policy": best.ID, "org": m.Tenant, "from": from, "to": d.Effort,
						"reasoning_used": used, "reasoning_budget": best.ReasoningBudget})
				}
			}
		}
	}
	return d, true
}

// RecordReasoning 은 정책별 일 추론 토큰을 더한다 (BL-COMP-05).
func (c *Compute) RecordReasoning(ctx context.Context, policy string, tokens int) {
	if policy == "" || tokens <= 0 {
		return
	}
	k := usageKey(policy, c.Now())
	c.R.IncrBy(ctx, k, int64(tokens))
	c.R.Expire(ctx, k, 48*time.Hour)
}

func (c *Compute) Usage(ctx context.Context, id string) map[string]any {
	used, _ := c.R.Get(ctx, usageKey(id, c.Now())).Int()
	for _, p := range c.List(ctx) {
		if p.ID == id {
			out := map[string]any{"policy": id, "day": c.Now().UTC().Format("2006-01-02"), "reasoning_tokens": used, "budget": p.ReasoningBudget}
			if p.ReasoningBudget > 0 {
				out["pct"], _ = strconv.ParseFloat(fmt.Sprintf("%.1f", float64(used)*100/float64(p.ReasoningBudget)), 64)
			}
			return out
		}
	}
	return nil
}

func indexOf(xs []string, v string) int {
	for i, x := range xs {
		if x == v {
			return i
		}
	}
	return -1
}
