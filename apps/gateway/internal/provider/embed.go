package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"hash/fnv"
	"io"
	"math"
	"net/http"
	"strings"
	"unicode"
)

// Embedder 는 임베딩을 지원하는 공급자다 (X-KB-02 kb.embed.model — 게이트웨이 alias 경유).
type Embedder interface {
	Embed(ctx context.Context, model string, inputs []string, dims int) ([][]float32, int, error)
}

// embedTerms: 단어 + 한글 음절 2-gram(어미 변화에도 겹치게).
func embedTerms(s string) []string {
	var out []string
	for _, w := range strings.FieldsFunc(strings.ToLower(s), func(r rune) bool { return !unicode.IsLetter(r) && !unicode.IsDigit(r) }) {
		out = append(out, "w:"+w)
		rs := []rune(w)
		if len(rs) >= 2 && unicode.Is(unicode.Hangul, rs[0]) {
			for i := 0; i+1 < len(rs); i++ {
				out = append(out, "b:"+string(rs[i:i+2]))
			}
		}
	}
	return out
}

// Embed (mock) 는 해싱 트릭 결정적 벡터다 — 어휘가 겹칠수록 코사인이 크다. L2 정규화.
func (Mock) Embed(_ context.Context, model string, inputs []string, dims int) ([][]float32, int, error) {
	if !strings.HasPrefix(model, "mock-embed") {
		return nil, 0, &Error{Status: http.StatusBadRequest, Msg: "임베딩 모델 아님: " + model}
	}
	if dims <= 0 {
		dims = 256
	}
	out := make([][]float32, len(inputs))
	tokens := 0
	for i, s := range inputs {
		v := make([]float64, dims)
		for _, t := range embedTerms(s) {
			h := fnv.New64a()
			_, _ = h.Write([]byte(t))
			x := h.Sum64()
			sign := 1.0
			if x&1 == 1 {
				sign = -1
			}
			w := 1.0
			if strings.HasPrefix(t, "b:") {
				w = 0.5
			}
			v[(x>>1)%uint64(dims)] += sign * w
		}
		n := 0.0
		for _, f := range v {
			n += f * f
		}
		n = math.Sqrt(n)
		vec := make([]float32, dims)
		for j, f := range v {
			if n > 0 {
				vec[j] = float32(f / n)
			}
		}
		out[i] = vec
		tokens += EstimateTokens(s)
	}
	return out, tokens, nil
}

// Embed (OpenAI 호환) — dimensions 지원 모델이면 차원을 맞춘다.
func (o OpenAI) Embed(ctx context.Context, model string, inputs []string, dims int) ([][]float32, int, error) {
	key, idx, ok := o.Pool.Pick()
	if !ok {
		return nil, 0, &Error{Status: http.StatusServiceUnavailable, Msg: "사용 가능한 업스트림 키 없음"}
	}
	body := map[string]any{"model": model, "input": inputs}
	if dims > 0 {
		body["dimensions"] = dims
	}
	b, _ := json.Marshal(body)
	hr, _ := http.NewRequestWithContext(ctx, http.MethodPost, o.BaseURL+"/embeddings", bytes.NewReader(b))
	hr.Header.Set("Content-Type", "application/json")
	hr.Header.Set("Authorization", "Bearer "+key)
	res, err := o.Client.Do(hr)
	if err != nil {
		return nil, 0, err
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(res.Body, 32<<20))
	o.Pool.Report(idx, res.StatusCode)
	if res.StatusCode != http.StatusOK {
		return nil, 0, &Error{Status: res.StatusCode, Msg: string(raw[:min(len(raw), 300)])}
	}
	var out struct {
		Data []struct {
			Embedding []float32 `json:"embedding"`
			Index     int       `json:"index"`
		} `json:"data"`
		Usage struct {
			Prompt int `json:"prompt_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(raw, &out); err != nil || len(out.Data) != len(inputs) {
		return nil, 0, &Error{Status: http.StatusBadGateway, Msg: "임베딩 응답 파싱 실패"}
	}
	vecs := make([][]float32, len(inputs))
	for _, d := range out.Data {
		vecs[d.Index] = d.Embedding
	}
	return vecs, out.Usage.Prompt, nil
}
