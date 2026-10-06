package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
)

// OpenAI 는 OpenAI 호환 /chat/completions 업스트림이다 (vLLM · 사내 프록시 포함).
type OpenAI struct {
	BaseURL string
	Pool    *KeyPool
	Client  *http.Client
}

func (OpenAI) Name() string { return "openai" }

func (o OpenAI) Complete(ctx context.Context, req Request) (Response, error) {
	key, idx, ok := o.Pool.Pick()
	if !ok {
		return Response{}, &Error{Status: http.StatusServiceUnavailable, Msg: "사용 가능한 업스트림 키 없음"}
	}
	body := map[string]any{"model": req.Model, "messages": req.Messages}
	if req.MaxTokens > 0 {
		body["max_tokens"] = req.MaxTokens
	}
	if req.Temperature != nil {
		body["temperature"] = *req.Temperature
	}
	if req.ResponseFormat != "" {
		body["response_format"] = map[string]string{"type": req.ResponseFormat}
	}
	if len(req.Tools) > 0 {
		body["tools"] = req.Tools
		if len(req.ToolChoice) > 0 {
			body["tool_choice"] = req.ToolChoice
		}
	}
	if req.CacheKey != "" { // OpenAI 자동 prefix 캐시의 라우팅 힌트 (EC-CACHE-01)
		body["prompt_cache_key"] = req.CacheKey
	}
	if req.Effort != "" {
		body["reasoning_effort"] = req.Effort
	}
	b, _ := json.Marshal(body)
	hr, _ := http.NewRequestWithContext(ctx, http.MethodPost, o.BaseURL+"/chat/completions", bytes.NewReader(b))
	hr.Header.Set("Content-Type", "application/json")
	hr.Header.Set("Authorization", "Bearer "+key)
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(hr.Header)) // trace 를 업스트림(vLLM 등)까지 전파
	res, err := o.Client.Do(hr)
	if err != nil {
		return Response{}, err
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(res.Body, 8<<20))
	o.Pool.Report(idx, res.StatusCode)
	if res.StatusCode != http.StatusOK {
		return Response{}, &Error{Status: res.StatusCode, Msg: string(raw[:min(len(raw), 300)])}
	}
	var out struct {
		Choices []struct {
			Message      Message `json:"message"`
			FinishReason string  `json:"finish_reason"`
		} `json:"choices"`
		Usage *struct {
			Prompt        int `json:"prompt_tokens"`
			Completion    int `json:"completion_tokens"`
			PromptDetails struct {
				Cached int `json:"cached_tokens"`
			} `json:"prompt_tokens_details"`
			CompletionDetails struct {
				Reasoning int `json:"reasoning_tokens"`
			} `json:"completion_tokens_details"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(raw, &out); err != nil || len(out.Choices) == 0 {
		return Response{}, &Error{Status: http.StatusBadGateway, Msg: "업스트림 응답 파싱 실패"}
	}
	r := Response{Content: out.Choices[0].Message.Content, ToolCalls: out.Choices[0].Message.ToolCalls, FinishReason: out.Choices[0].FinishReason}
	if u := out.Usage; u != nil {
		r.Usage = Usage{Prompt: u.Prompt, Completion: u.Completion, Cached: u.PromptDetails.Cached, Reasoning: u.CompletionDetails.Reasoning}
	} else {
		r.Usage = Usage{Prompt: EstimateTokens(PromptText(req.Messages)), Completion: EstimateTokens(r.Content), Estimated: true}
	}
	return r, nil
}

// Probe 는 키 하나로 GET {base}/models 를 불러 유효성을 본다 (키 회전 검증).
func (o OpenAI) Probe(ctx context.Context, key string) error {
	hr, _ := http.NewRequestWithContext(ctx, http.MethodGet, o.BaseURL+"/models", nil)
	hr.Header.Set("Authorization", "Bearer "+key)
	res, err := o.Client.Do(hr)
	if err != nil {
		return err
	}
	res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return fmt.Errorf("upstream /models %d", res.StatusCode)
	}
	return nil
}

// KeyPool 은 업스트림 키 풀이다 (FR-GW-KEY-01·06): 라운드로빈 분산, 401/403 연속 3회 → disabled.
type KeyPool struct {
	mu        sync.Mutex
	keys      []poolKey
	next      int
	Unhealthy func(provider string, index int) // key.unhealthy 이벤트 훅
	provider  string
}

type poolKey struct {
	secret   string
	status   string // active | retiring(회전 유예 중 — 활성 키가 없을 때만 사용) | disabled
	authFail int
	since    time.Time
}

type KeyStatus struct {
	Index    int    `json:"index"`
	Masked   string `json:"masked"`
	Status   string `json:"status"`
	AuthFail int    `json:"authFailures"`
}

func NewKeyPool(provider string, secrets []string) *KeyPool {
	p := &KeyPool{provider: provider}
	for _, s := range secrets {
		if s != "" {
			p.keys = append(p.keys, poolKey{secret: s, status: "active", since: time.Now()})
		}
	}
	return p
}

func (p *KeyPool) Pick() (string, int, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, want := range []string{"active", "retiring"} {
		for range p.keys {
			i := p.next % len(p.keys)
			p.next++
			if p.keys[i].status == want {
				return p.keys[i].secret, i, true
			}
		}
	}
	return "", -1, false
}

// Replace 는 키 집합을 무중단으로 교체한다 (FR-PLT-SEC-04): 진행 중 요청은 이전 슬라이스로 끝나고,
// 같은 키의 disabled 상태는 유지, retiring 은 유예 동안만 예비로 쓴다.
func (p *KeyPool) Replace(active, retiring []string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	old := map[string]poolKey{}
	for _, k := range p.keys {
		old[k.secret] = k
	}
	var keys []poolKey
	add := func(s, status string) {
		if s == "" {
			return
		}
		k, ok := old[s]
		if !ok || k.status != "disabled" {
			k = poolKey{secret: s, status: status, since: time.Now()}
		}
		keys = append(keys, k)
	}
	for _, s := range active {
		add(s, "active")
	}
	for _, s := range retiring {
		add(s, "retiring")
	}
	p.keys, p.next = keys, 0
}

// Secrets 는 현재 키 평문 목록이다(회전 시 이전 슬롯으로 옮기기 위한 내부용 — 응답에 싣지 않는다).
func (p *KeyPool) Secrets(status string) []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	var out []string
	for _, k := range p.keys {
		if k.status == status {
			out = append(out, k.secret)
		}
	}
	return out
}

func (p *KeyPool) Report(i, status int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if i < 0 || i >= len(p.keys) {
		return
	}
	k := &p.keys[i]
	if status == http.StatusUnauthorized || status == http.StatusForbidden {
		k.authFail++
		if k.authFail >= 3 && k.status == "active" {
			k.status, k.since = "disabled", time.Now()
			slog.Warn("upstream key disabled", "provider", p.provider, "index", i)
			if p.Unhealthy != nil {
				go p.Unhealthy(p.provider, i)
			}
		}
		return
	}
	k.authFail = 0
}

// Compromise 는 유출 신고된 키를 즉시 제외한다.
func (p *KeyPool) Compromise(i int) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if i < 0 || i >= len(p.keys) {
		return fmt.Errorf("key index %d 없음", i)
	}
	p.keys[i].status, p.keys[i].since = "disabled", time.Now()
	return nil
}

func (p *KeyPool) Status() []KeyStatus {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]KeyStatus, len(p.keys))
	for i, k := range p.keys {
		m := "****"
		if len(k.secret) > 8 {
			m = k.secret[:4] + "…" + k.secret[len(k.secret)-4:]
		}
		out[i] = KeyStatus{Index: i, Masked: m, Status: k.status, AuthFail: k.authFail}
	}
	return out
}
