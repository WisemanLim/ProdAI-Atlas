package provider

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

// Mock 은 결정적 시험 공급자다. 모델명으로 동작을 고른다:
// mock-chat(에코) · mock-planner(json_object 면 계획 JSON) · mock-agent(도구 호출 각본) · mock-503 / mock-429(오류 주입).
type Mock struct{ Latency time.Duration }

func (Mock) Name() string { return "mock" }

var planSplit = regexp.MustCompile(`\n+|(?:[.!?。])\s+|\s*(?:;|→|그리고|그다음|그 다음)\s*`)

func (m Mock) Complete(ctx context.Context, req Request) (Response, error) {
	if m.Latency > 0 {
		select {
		case <-time.After(m.Latency):
		case <-ctx.Done():
			return Response{}, ctx.Err()
		}
	}
	switch req.Model {
	case "mock-503":
		return Response{}, &Error{Status: http.StatusServiceUnavailable, Msg: "mock unavailable"}
	case "mock-429":
		return Response{}, &Error{Status: http.StatusTooManyRequests, Msg: "mock rate limited"}
	}
	if strings.HasPrefix(req.Model, "mock-judge") {
		return mockJudge(req), nil
	}
	if req.Model == "mock-agent" {
		r := mockAgent(req)
		mockCache(req, &r.Usage)
		return r, nil
	}
	last := ""
	for i := len(req.Messages) - 1; i >= 0; i-- {
		if req.Messages[i].Role == "user" {
			last = req.Messages[i].Content
			break
		}
	}
	content := "[mock:" + req.Model + "] " + last
	if req.Seed != nil && req.Temperature != nil && *req.Temperature > 0 { // 샘플링 다양성 흉내 (best-of-n, BL-COMP-03): 시드별 결정적 변형
		h := sha256.Sum256([]byte(fmt.Sprintf("%d\x00%s", *req.Seed, last)))
		content += fmt.Sprintf(" [s=%d]", int(h[0])%10)
	}
	prose := false // 시스템 프롬프트 지시 흉내: "Reply in prose" 면 JSON 대신 서술 (프롬프트 게시 게이트 시험, BL-HARN-06)
	for _, m := range req.Messages {
		if m.Role == "system" && strings.Contains(m.Content, "Reply in prose") {
			prose = true
		}
	}
	if req.ResponseFormat == "json_object" && prose && strings.HasPrefix(req.Model, "mock-planner") {
		content = "계획: 먼저 살펴보고 차근차근 진행하겠습니다."
	} else if req.ResponseFormat == "json_object" {
		var steps []map[string]any
		for _, p := range planSplit.Split(last, -1) {
			if p = strings.Trim(p, " ,."); p != "" {
				deps := []int{}
				if len(steps) > 0 {
					deps = append(deps, len(steps))
				}
				steps = append(steps, map[string]any{"title": p, "depends_on": deps})
			}
		}
		b, _ := json.Marshal(map[string]any{"steps": steps, "dod": []map[string]string{{"type": "test", "spec": "모든 단계의 산출물이 검증됨"}}})
		content = string(b)
		if req.Model == "mock-planner-sloppy" { // 약한 모델 흉내: JSON 앞뒤에 군말 (하네스 보정 시험, BL-HARN-04·05)
			content = "물론입니다! 계획은 다음과 같습니다:\n" + content + "\n도움이 되었길 바랍니다."
		}
	}
	if req.MaxTokens > 0 && EstimateTokens(content) > req.MaxTokens && req.ResponseFormat == "" {
		r := []rune(content)
		content = string(r[:min(len(r), req.MaxTokens*4)])
		return Response{Content: content, FinishReason: "length", Usage: Usage{Prompt: EstimateTokens(PromptText(req.Messages)), Completion: req.MaxTokens}}, nil
	}
	r := Response{Content: content, FinishReason: "stop",
		Usage: Usage{Prompt: EstimateTokens(PromptText(req.Messages)), Completion: EstimateTokens(content)}}
	if req.ThinkingBudget > 0 || req.Effort != "" { // 추론 토큰 시뮬레이션: 예산의 1/4 사용
		r.Usage.Reasoning = req.ThinkingBudget / 4
		r.Usage.Completion += r.Usage.Reasoning
	}
	mockCache(req, &r.Usage)
	return r, nil
}

// mockCache 는 공급사 prompt cache 를 흉내 낸다 (Anthropic 형): 캐시 키가 있고 prefix(마지막 user 메시지 앞 전체)가
// 이전과 같으면 그 토큰은 캐시 읽기, 처음이면 캐시 쓰기로 보고한다. TTL 이 지나면 다시 쓰기.
var (
	mockCacheMu sync.Mutex
	mockCacheAt = map[string]time.Time{}
)

func mockCache(req Request, u *Usage) {
	if req.CacheKey == "" {
		return
	}
	last := len(req.Messages) - 1
	for last >= 0 && req.Messages[last].Role != "user" {
		last--
	}
	if last <= 0 {
		return
	}
	prefix := PromptText(req.Messages[:last]) + strings.Join(req.ToolNames(), ",")
	n := EstimateTokens(prefix)
	h := sha256.Sum256([]byte(req.CacheKey + "\x00" + prefix))
	k := hex.EncodeToString(h[:])
	ttl := 5 * time.Minute
	if req.CacheTTL == "1h" {
		ttl = time.Hour
	}
	mockCacheMu.Lock()
	defer mockCacheMu.Unlock()
	if at, ok := mockCacheAt[k]; ok && time.Since(at) < ttl {
		u.Cached = min(n, u.Prompt)
	} else {
		u.CacheWrite = min(n, u.Prompt)
	}
	mockCacheAt[k] = time.Now() // 읽기마다 TTL 갱신(공급사 동작과 같음)
}

var toolArgs = regexp.MustCompile(`^\((\{.*?\})\)`)

// mockAgent 는 루프 시험용 결정적 도구 호출 각본이다. 목표(첫 user 메시지)에 언급된 도구를 호출하고,
// 도구 결과를 받으면 최종 답을 낸다. 지시어: "repeat-forever"(같은 호출 반복) · "never-finish"(인자 바꿔 계속 호출).
//
//	예) "atlas_calc({"expr":"6*7"}) 로 계산하고 atlas_echo 로 확인"
func mockAgent(req Request) Response {
	goal, toolMsgs, lastRole := "", 0, ""
	var results []string
	for _, m := range req.Messages {
		if m.Role == "user" && goal == "" {
			goal = m.Content
		}
		if m.Role == "tool" {
			toolMsgs++
			results = append(results, m.Content)
		}
		lastRole = m.Role
	}
	type mention struct {
		pos  int
		name string
		args string
	}
	var ms []mention
	for _, n := range req.ToolNames() {
		if i := strings.Index(goal, n); i >= 0 {
			args := "{}"
			if m := toolArgs.FindStringSubmatch(goal[i+len(n):]); m != nil {
				args = m[1]
			}
			ms = append(ms, mention{i, n, args})
		}
	}
	sort.Slice(ms, func(a, b int) bool { return ms[a].pos < ms[b].pos })
	call := func(ms []mention, seq int) Response {
		r := Response{FinishReason: "tool_calls"}
		for i, m := range ms {
			tc := ToolCall{ID: fmt.Sprintf("call_%d_%d", seq, i), Type: "function"}
			tc.Function.Name, tc.Function.Arguments = m.name, m.args
			r.ToolCalls = append(r.ToolCalls, tc)
		}
		r.Usage = Usage{Prompt: EstimateTokens(PromptText(req.Messages)), Completion: 8 * len(ms)}
		return r
	}
	switch {
	case len(ms) > 0 && strings.Contains(goal, "repeat-forever"):
		return call(ms[:1], toolMsgs)
	case len(ms) > 0 && strings.Contains(goal, "never-finish"):
		m := ms[0]
		m.args = fmt.Sprintf(`{"n":%d}`, toolMsgs)
		return call([]mention{m}, toolMsgs)
	case len(ms) > 0 && lastRole != "tool" && toolMsgs == 0:
		return call(ms, 0)
	}
	content := "[mock:final] " + goal
	if len(results) > 0 {
		content = "[mock:final] " + strings.Join(results, " | ")
	}
	return Response{Content: content, FinishReason: "stop",
		Usage: Usage{Prompt: EstimateTokens(PromptText(req.Messages)), Completion: EstimateTokens(content)}}
}
