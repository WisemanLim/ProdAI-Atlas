// Package config 는 게이트웨이 라우팅·가격·정책 설정을 읽는다.
package config

import (
	_ "embed"
	"fmt"
	"os"
	"sort"

	"gopkg.in/yaml.v3"
)

//go:embed gateway.yaml
var defaultYAML []byte

type Deployment struct {
	Provider string `yaml:"provider" json:"provider"`
	Model    string `yaml:"model" json:"model"`
	Weight   int    `yaml:"weight" json:"weight"`
}

type Alias struct {
	Deployments   []Deployment `yaml:"deployments"`
	Fallbacks     []string     `yaml:"fallbacks"`
	Tier          string       `yaml:"tier"`            // small | medium | large (EC-MODEL-01)
	DefaultMaxOut int          `yaml:"default_max_out"` // max_tokens 누락 시 주입값 (EC-CTRL-02)
	Version       int          `yaml:"-"`               // 런타임 오버레이 버전 (0 = 설정 파일)
}

type PriceVersion struct {
	EffectiveFrom string `yaml:"effective_from"` // YYYY-MM-DD
	Input         string `yaml:"input"`          // USD / 1M tokens (decimal string)
	Output        string `yaml:"output"`
	CachedInput   string `yaml:"cached_input"` // 캐시 읽기 단가 (EC-CACHE-03). 비면 input 과 같음
	CacheWrite    string `yaml:"cache_write"`  // 캐시 쓰기 단가. 비면 input 과 같음
}

// ModelCard 는 모델 카탈로그 항목이다 (FR-EC-MODEL-07). 가격은 prices 를 참조한다.
type ModelCard struct {
	Provider         string   `yaml:"provider" json:"provider"`
	ContextWindow    int      `yaml:"context_window" json:"context_window"`
	MaxOutput        int      `yaml:"max_output" json:"max_output"`
	Modalities       []string `yaml:"modalities" json:"modalities"`
	Tools            bool     `yaml:"tools" json:"tools"`
	StructuredOutput bool     `yaml:"structured_output" json:"structured_output"`
	Reasoning        bool     `yaml:"reasoning" json:"reasoning"`
	Regions          []string `yaml:"regions" json:"regions"`
	Tokenizer        string   `yaml:"tokenizer" json:"tokenizer"`
	DeprecationDate  string   `yaml:"deprecation_date" json:"deprecation_date,omitempty"` // YYYY-MM-DD
}

// TaskPolicy 는 과업 유형별 모델·출력·추론 정책이다 (FR-EC-MODEL-01·02, EC-CTRL-02).
type TaskPolicy struct {
	TaskType     string `yaml:"task_type"`
	Tier         string `yaml:"tier"`
	DefaultAlias string `yaml:"default_alias"`
	MaxTier      string `yaml:"max_tier"`
	MaxEffort    string `yaml:"max_effort"`
	MaxTokens    int    `yaml:"max_tokens"`
}

// RouteRule 은 규칙 기반 비용 라우팅이다 (FR-EC-ROUTE-01). 위에서부터 첫 일치 규칙을 적용한다.
type RouteRule struct {
	Name string   `yaml:"name"`
	From []string `yaml:"from"` // 대상 alias glob
	To   string   `yaml:"to"`
	When struct {
		MaxInputTokens int               `yaml:"max_input_tokens"`
		NoTools        bool              `yaml:"no_tools"`
		TaskType       string            `yaml:"task_type"`
		Tags           map[string]string `yaml:"tags"`
	} `yaml:"when"`
}

type Econ struct {
	ModelPolicy   []TaskPolicy   `yaml:"model_policy"`
	Routes        []RouteRule    `yaml:"routes"`
	EffortBudgets map[string]int `yaml:"effort_budgets"` // minimal·low·medium·high → thinking budget_tokens
	PromptCache   struct {
		MinTokens int `yaml:"min_tokens"`
	} `yaml:"prompt_cache"`
	ExactCache struct {
		Enabled bool `yaml:"enabled"`
		TTLSec  int  `yaml:"ttl_sec"`
	} `yaml:"exact_cache"`
	TaskBudgetTokens int    `yaml:"task_budget_tokens"`
	SoftPct          int    `yaml:"soft_pct"`
	BatchDiscount    string `yaml:"batch_discount"`
	Batch            struct {
		PollBaseSec  int `yaml:"poll_base_sec"`
		PollMaxSec   int `yaml:"poll_max_sec"`
		MockDelaySec int `yaml:"mock_delay_sec"`
		MaxRows      int `yaml:"max_rows"`
	} `yaml:"batch"`
}

// Tiers 는 등급 순서다.
var Tiers = map[string]int{"small": 1, "medium": 2, "large": 3}

// Efforts 는 추론 노력 순서다.
var Efforts = map[string]int{"minimal": 0, "low": 1, "medium": 2, "high": 3}

type Policy struct {
	MaxTokensCap     int      `yaml:"max_tokens_cap"`
	DefaultMaxTokens int      `yaml:"default_max_tokens"`
	DeniedParams     []string `yaml:"denied_params"`
	PayloadLogging   string   `yaml:"payload_logging"`
}

type Retry struct {
	MaxAttempts int `yaml:"max_attempts"`
	BaseMS      int `yaml:"base_ms"`
	MaxMS       int `yaml:"max_ms"`
}

type Circuit struct {
	WindowSec    int     `yaml:"window_sec"`
	MinRequests  int     `yaml:"min_requests"`
	FailureRatio float64 `yaml:"failure_ratio"`
	CooldownSec  int     `yaml:"cooldown_sec"`
}

type Config struct {
	Aliases map[string]Alias          `yaml:"aliases"`
	Prices  map[string][]PriceVersion `yaml:"prices"`
	Policy  Policy                    `yaml:"policy"`
	Retry   Retry                     `yaml:"retry"`
	Circuit Circuit                   `yaml:"circuit"`
	Models  map[string]ModelCard      `yaml:"models"`
	Econ    Econ                      `yaml:"econ"`
}

// Load 는 path 가 비면 내장 기본값을 읽는다.
func Load(path string) (*Config, error) {
	b := defaultYAML
	if path != "" {
		var err error
		if b, err = os.ReadFile(path); err != nil {
			return nil, err
		}
	}
	var c Config
	if err := yaml.Unmarshal(b, &c); err != nil {
		return nil, err
	}
	return &c, c.validate()
}

func (c *Config) validate() error {
	for name, a := range c.Aliases {
		if len(a.Deployments) == 0 {
			return fmt.Errorf("alias %s: deployments 비어 있음", name)
		}
		for _, f := range a.Fallbacks {
			if _, ok := c.Aliases[f]; !ok {
				return fmt.Errorf("alias %s: 알 수 없는 fallback %s", name, f)
			}
		}
	}
	for _, r := range c.Econ.Routes {
		if _, ok := c.Aliases[r.To]; !ok {
			return fmt.Errorf("econ.routes %s: 알 수 없는 alias %s", r.Name, r.To)
		}
	}
	for _, p := range c.Econ.ModelPolicy {
		if _, ok := c.Aliases[p.DefaultAlias]; p.DefaultAlias != "" && !ok {
			return fmt.Errorf("econ.model_policy %s: 알 수 없는 alias %s", p.TaskType, p.DefaultAlias)
		}
	}
	switch c.Policy.PayloadLogging {
	case "off", "masked", "full":
	default:
		return fmt.Errorf("policy.payload_logging: off | masked | full")
	}
	return nil
}

// TaskPolicyFor 는 과업 유형 정책을 찾는다.
func (c *Config) TaskPolicyFor(taskType string) (TaskPolicy, bool) {
	for _, p := range c.Econ.ModelPolicy {
		if p.TaskType == taskType {
			return p, true
		}
	}
	return TaskPolicy{}, false
}

// AliasNames 는 정렬된 alias 목록이다.
func (c *Config) AliasNames() []string {
	out := make([]string, 0, len(c.Aliases))
	for n := range c.Aliases {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}
