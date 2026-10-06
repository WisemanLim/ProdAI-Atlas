// Package econ 은 추론 경제성 레버다 (PRD §12.7 EC-*): 규칙 라우팅 · 과업 유형별 모델 정책 · reasoning effort 정규화 ·
// max_tokens 정책 · 계층 예산 · 과업 토큰 예산 · prompt cache 지시 · exact 응답 캐시 · 배치 · 비용 시뮬레이터.
package econ

import (
	"fmt"
	"path"
	"time"

	"atlas-gateway/internal/config"
)

type PlanInput struct {
	Alias       string
	TaskType    string
	Tags        map[string]string
	InputTokens int
	HasTools    bool
	MaxTokens   *int
	Effort      string
	Pinned      bool              // x-atlas-route-pin: 규칙 라우팅·정책 하향을 건너뜀
	Allows      func(string) bool // 가상키 허용 alias
}

type PlanOutput struct {
	Alias          string
	Rule           string // 적용된 라우팅 규칙
	TaskPolicy     string
	MaxTokens      int
	Effort         string
	ThinkingBudget int
	Mutations      []string
}

// Card 는 alias 의 첫 배포 모델 카드다.
func Card(cfg *config.Config, alias string) (config.ModelCard, string) {
	a := cfg.Aliases[alias]
	if len(a.Deployments) == 0 {
		return config.ModelCard{}, ""
	}
	m := a.Deployments[0].Model
	return cfg.Models[m], m
}

func tier(cfg *config.Config, alias string) int { return config.Tiers[cfg.Aliases[alias].Tier] }

// Plan 은 요청 하나에 경제성 정책을 적용한다. 순서: auto 해석 → 규칙 라우팅(EC-ROUTE-01) → 과업 등급 상한(EC-MODEL-01)
// → max_tokens 주입·클램프(EC-CTRL-02) → effort 정규화(EC-MODEL-02).
func Plan(cfg *config.Config, in PlanInput) PlanOutput {
	out := PlanOutput{Alias: in.Alias}
	allow := func(a string) bool {
		_, known := cfg.Aliases[a]
		return known && (in.Allows == nil || in.Allows(a))
	}
	tp, hasTP := cfg.TaskPolicyFor(in.TaskType)
	if hasTP {
		out.TaskPolicy = tp.TaskType
	}
	if out.Alias == "auto" {
		target := "chat-default"
		if hasTP && tp.DefaultAlias != "" {
			target = tp.DefaultAlias
		}
		out.Alias = target
		out.Mutations = append(out.Mutations, "model_policy:auto->"+target)
	}
	if !in.Pinned {
		for _, r := range cfg.Econ.Routes {
			if matchRule(r, out.Alias, in) && r.To != out.Alias && allow(r.To) {
				out.Mutations = append(out.Mutations, fmt.Sprintf("econ_route:%s:%s->%s", r.Name, out.Alias, r.To))
				out.Alias, out.Rule = r.To, r.Name
				break
			}
		}
		if hasTP && tp.MaxTier != "" && tier(cfg, out.Alias) > config.Tiers[tp.MaxTier] && allow(tp.DefaultAlias) {
			out.Mutations = append(out.Mutations, fmt.Sprintf("model_policy:%s:%s->%s", tp.TaskType, out.Alias, tp.DefaultAlias))
			out.Alias = tp.DefaultAlias
		}
	}

	// max_tokens: 누락 시 alias 기본값 주입, 전역·과업 상한으로 클램프
	if in.MaxTokens != nil {
		out.MaxTokens = *in.MaxTokens
	} else {
		out.MaxTokens = cfg.Aliases[out.Alias].DefaultMaxOut
		if out.MaxTokens == 0 {
			out.MaxTokens = cfg.Policy.DefaultMaxTokens
		}
		out.Mutations = append(out.Mutations, fmt.Sprintf("max_tokens:default=%d", out.MaxTokens))
	}
	for _, cap := range []int{cfg.Policy.MaxTokensCap, map[bool]int{true: tp.MaxTokens}[hasTP]} {
		if cap > 0 && out.MaxTokens > cap {
			out.Mutations = append(out.Mutations, fmt.Sprintf("max_tokens:%d->%d", out.MaxTokens, cap))
			out.MaxTokens = cap
		}
	}

	// effort: reasoning 모델에만 전달, 과업 상한으로 클램프, thinking 예산으로 환산
	card, _ := Card(cfg, out.Alias)
	effort := in.Effort
	if _, ok := config.Efforts[effort]; effort != "" && !ok {
		out.Mutations = append(out.Mutations, "effort:invalid:"+effort)
		effort = ""
	}
	switch {
	case effort != "" && !card.Reasoning:
		out.Mutations = append(out.Mutations, "effort:ignored(non-reasoning)")
		effort = ""
	case effort == "" && card.Reasoning:
		effort = "low" // 보이지 않는 thinking 낭비 방지 — 명시하지 않으면 low
		out.Mutations = append(out.Mutations, "effort:default=low")
	}
	if effort != "" && hasTP && tp.MaxEffort != "" && config.Efforts[effort] > config.Efforts[tp.MaxEffort] {
		out.Mutations = append(out.Mutations, fmt.Sprintf("effort:%s->%s", effort, tp.MaxEffort))
		effort = tp.MaxEffort
	}
	out.Effort = effort
	if effort != "" {
		out.ThinkingBudget = cfg.Econ.EffortBudgets[effort]
	}
	return out
}

func matchRule(r config.RouteRule, alias string, in PlanInput) bool {
	from := false
	for _, g := range r.From {
		if ok, _ := path.Match(g, alias); ok {
			from = true
			break
		}
	}
	if !from {
		return false
	}
	w := r.When
	if w.MaxInputTokens > 0 && in.InputTokens >= w.MaxInputTokens {
		return false
	}
	if w.NoTools && in.HasTools {
		return false
	}
	if w.TaskType != "" && w.TaskType != in.TaskType {
		return false
	}
	for k, v := range w.Tags {
		if in.Tags[k] != v {
			return false
		}
	}
	return true
}

// CatalogEntry 는 카탈로그 응답 항목이다 (폐기 60일 전 경고 포함).
type CatalogEntry struct {
	Model string `json:"model"`
	config.ModelCard
	Aliases       []string          `json:"aliases"`
	Prices        map[string]string `json:"prices_per_1m,omitempty"`
	DeprecationIn *int              `json:"deprecation_in_days,omitempty"`
	Warning       string            `json:"warning,omitempty"`
}

func Catalog(cfg *config.Config, now time.Time, prices func(model string) map[string]string) []CatalogEntry {
	byModel := map[string][]string{}
	for _, a := range cfg.AliasNames() {
		for _, d := range cfg.Aliases[a].Deployments {
			byModel[d.Model] = append(byModel[d.Model], a)
		}
	}
	out := []CatalogEntry{}
	for _, m := range sortedKeys(cfg.Models) {
		e := CatalogEntry{Model: m, ModelCard: cfg.Models[m], Aliases: byModel[m], Prices: prices(m)}
		if d, err := time.Parse("2006-01-02", e.DeprecationDate); err == nil {
			days := int(d.Sub(now).Hours() / 24)
			e.DeprecationIn = &days
			if days <= 60 {
				e.Warning = fmt.Sprintf("폐기 예정 %d일 전 — 대체 모델로 이전 필요", days)
			}
		}
		out = append(out, e)
	}
	return out
}
