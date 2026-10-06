// Package provider 는 업스트림 모델 공급자 어댑터다 (FR-GW-ROUTE-02). M0: mock · OpenAI 호환.
package provider

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"unicode/utf8"
)

type Message struct {
	Role       string     `json:"role"`
	Content    string     `json:"content"`
	ToolCalls  []ToolCall `json:"tool_calls,omitempty"`   // assistant 의 도구 호출
	ToolCallID string     `json:"tool_call_id,omitempty"` // role=tool 응답이 가리키는 호출
	Name       string     `json:"name,omitempty"`
}

// ToolCall 은 OpenAI 형식 함수 호출이다 (arguments 는 JSON 문자열).
type ToolCall struct {
	ID       string `json:"id"`
	Type     string `json:"type"`
	Function struct {
		Name      string `json:"name"`
		Arguments string `json:"arguments"`
	} `json:"function"`
}

type Request struct {
	Model          string
	Messages       []Message
	MaxTokens      int
	Temperature    *float64
	ResponseFormat string            // "", "json_object"
	Tools          []json.RawMessage // OpenAI tools[] 그대로 전달
	ToolChoice     json.RawMessage
	// 경제성 (EC-CACHE-01 · EC-MODEL-02) — 게이트웨이가 정규화해 공급자 형식으로 변환한다
	CacheKey       string // 공급사 prompt cache 키(OpenAI prompt_cache_key). 비면 캐시 지시 없음
	CacheTTL       string // "5m" | "1h" (Anthropic cache_control ttl)
	Effort         string // minimal | low | medium | high → OpenAI reasoning_effort
	ThinkingBudget int    // Anthropic thinking.budget_tokens
	// 라우팅 보조 (공급자는 무시)
	AssignKey string // 카나리 고정 배정 키 (x-atlas-assign-key, BL-MODEL-05)
	Seed      *int   // 샘플링 시드 (best-of-n 후보 다양화, BL-COMP-03)
}

// ToolNames 는 tools[] 의 function.name 목록이다.
func (r Request) ToolNames() []string {
	var out []string
	for _, t := range r.Tools {
		var v struct {
			Function struct{ Name string } `json:"function"`
		}
		if json.Unmarshal(t, &v) == nil && v.Function.Name != "" {
			out = append(out, v.Function.Name)
		}
	}
	return out
}

type Usage struct {
	Prompt     int  `json:"prompt_tokens"` // 캐시 읽기·쓰기 토큰 포함 전체 입력
	Completion int  `json:"completion_tokens"`
	Cached     int  `json:"-"` // 캐시 읽기 토큰 (OpenAI prompt_tokens_details.cached_tokens)
	CacheWrite int  `json:"-"` // 캐시 쓰기 토큰 (Anthropic cache_creation_input_tokens)
	Reasoning  int  `json:"-"` // 출력 중 추론 토큰
	Estimated  bool `json:"-"`
}

type Response struct {
	Content      string
	ToolCalls    []ToolCall
	FinishReason string
	Usage        Usage
}

type Provider interface {
	Name() string
	Complete(ctx context.Context, req Request) (Response, error)
}

// Error 는 업스트림 HTTP 오류다. Retriable 이면 재시도·폴백 대상이다.
type Error struct {
	Status int
	Msg    string
}

func (e *Error) Error() string { return fmt.Sprintf("upstream %d: %s", e.Status, e.Msg) }

// Retriable 은 429 · 5xx · timeout 을 재시도 대상으로 본다 (FR-GW-ROUTE-05).
func Retriable(err error) bool {
	var pe *Error
	if errors.As(err, &pe) {
		return pe.Status == http.StatusTooManyRequests || pe.Status >= 500
	}
	return errors.Is(err, context.DeadlineExceeded)
}

// EstimateTokens 는 토크나이저 없이 쓰는 근사(≈ 4 chars/token)다 — usage_estimated=true 로 표시된다.
func EstimateTokens(s string) int {
	n := utf8.RuneCountInString(s)
	if n == 0 {
		return 0
	}
	return (n + 3) / 4
}

func PromptText(msgs []Message) string {
	s := ""
	for _, m := range msgs {
		s += m.Role + ": " + m.Content + "\n"
		for _, tc := range m.ToolCalls {
			s += tc.Function.Name + tc.Function.Arguments + "\n"
		}
	}
	return s
}
