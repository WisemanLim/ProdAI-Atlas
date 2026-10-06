package server

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"atlas-gateway/internal/aliases"
	"atlas-gateway/internal/auth"
	"atlas-gateway/internal/econ"
)

func blSetup(t *testing.T) *fixture {
	f := setup(t, auth.VirtualKey{Tenant: "acme", Prefix: "sk-atlas-test-abcd", Models: []string{"*"}})
	f.s.Aliases = &aliases.Store{R: f.s.Redis, Cfg: f.s.Cfg, Now: time.Now}
	f.s.Compute = &econ.Compute{R: f.s.Redis, Now: time.Now, Publish: func(ctx context.Context, subj string, d any) { _ = f.rec.Publish(ctx, subj, d) }}
	return f
}

func chatModel(t *testing.T, f *fixture, alias string, hdr ...string) (string, map[string]string) {
	t.Helper()
	w := f.do("POST", "/v1/chat/completions", f.key, `{"model":"`+alias+`","messages":[{"role":"user","content":"hi"}]}`, hdr...)
	if w.Code != 200 {
		t.Fatalf("chat %s = %d %s", alias, w.Code, w.Body)
	}
	h := map[string]string{}
	for k := range w.Header() {
		h[strings.ToLower(k)] = w.Header().Get(k)
	}
	return h["x-atlas-route"], h
}

func TestAliasOverlayCanaryStickyAndRollback(t *testing.T) {
	f := blSetup(t)
	// 후보 0% 등록: 트래픽은 모두 기존 모델
	put := func(body string) map[string]any {
		w := f.do("PUT", "/admin/aliases/chat-default", admin, body)
		if w.Code != 200 {
			t.Fatalf("put = %d %s", w.Code, w.Body)
		}
		var m map[string]any
		_ = json.Unmarshal(w.Body.Bytes(), &m)
		return m
	}
	v1 := put(`{"deployments":[{"provider":"mock","model":"mock-chat","weight":100},{"provider":"mock","model":"mock-chat-small","weight":0}],"note":"candidate 0%"}`)
	for i := 0; i < 20; i++ {
		if r, _ := chatModel(t, f, "chat-default"); r != "mock/mock-chat" {
			t.Fatalf("0%% candidate served: %s", r)
		}
	}
	// 50% 카나리 + 고정 배정: 같은 키는 항상 같은 모델, 두 모델 모두 서빙
	v2 := put(`{"deployments":[{"provider":"mock","model":"mock-chat","weight":50},{"provider":"mock","model":"mock-chat-small","weight":50}]}`)
	seen := map[string]bool{}
	for i := 0; i < 40; i++ {
		key := fmt.Sprintf("user-%d", i)
		a, h := chatModel(t, f, "chat-default", "x-atlas-assign-key", key)
		b, _ := chatModel(t, f, "chat-default", "x-atlas-assign-key", key)
		if a != b || h["x-atlas-alias-version"] != "2" {
			t.Fatalf("sticky %s: %s vs %s (v%s)", key, a, b, h["x-atlas-alias-version"])
		}
		seen[a] = true
	}
	if !seen["mock/mock-chat"] || !seen["mock/mock-chat-small"] || v1["version"].(float64) != 1 || v2["version"].(float64) != 2 {
		t.Fatalf("split: %v", seen)
	}
	time.Sleep(50 * time.Millisecond) // 통계는 비동기 기록
	var st struct {
		Models []aliases.ModelStats `json:"models"`
	}
	_ = json.Unmarshal(f.do("GET", "/admin/aliases/chat-default/stats", admin, "").Body.Bytes(), &st)
	if len(st.Models) != 2 || st.Models[0].Requests+st.Models[1].Requests != 80 {
		t.Fatalf("stats: %+v", st.Models)
	}
	// 잘못된 alias 거부 · 롤백 v1 → 새 버전 3 이 v1 내용
	if w := f.do("PUT", "/admin/aliases/chat-default", admin, `{"deployments":[{"provider":"mock","model":"nope","weight":1}]}`); w.Code != 400 {
		t.Fatalf("unknown model = %d", w.Code)
	}
	if w := f.do("PUT", "/admin/aliases/chat-default", admin, `{"deployments":[{"provider":"mock","model":"mock-chat","weight":0}]}`); w.Code != 400 {
		t.Fatalf("zero weight = %d", w.Code)
	}
	w := f.do("POST", "/admin/aliases/chat-default/rollback", admin, `{"version":1,"by":"tester"}`)
	var rb aliases.Entry
	_ = json.Unmarshal(w.Body.Bytes(), &rb)
	if w.Code != 200 || rb.Version != 3 || rb.Deployments[1].Weight != 0 {
		t.Fatalf("rollback = %d %+v", w.Code, rb)
	}
	if r, _ := chatModel(t, f, "chat-default", "x-atlas-assign-key", "user-1"); r != "mock/mock-chat" {
		t.Fatalf("after rollback served %s", r)
	}
	// 새 alias(설정에 없음)도 오버레이로 생성·호출 가능, 삭제하면 404
	put2 := f.do("PUT", "/admin/aliases/chat-cand", admin, `{"deployments":[{"provider":"mock","model":"mock-chat-large","weight":1}],"fallbacks":["chat-default"]}`)
	if put2.Code != 200 {
		t.Fatalf("new alias = %d %s", put2.Code, put2.Body)
	}
	if r, _ := chatModel(t, f, "chat-cand"); r != "mock/mock-chat-large" {
		t.Fatalf("new alias served %s", r)
	}
	f.do("DELETE", "/admin/aliases/chat-cand", admin, "")
	if w := f.do("POST", "/v1/chat/completions", f.key, `{"model":"chat-cand","messages":[{"role":"user","content":"x"}]}`); w.Code != 404 {
		t.Fatalf("deleted alias = %d", w.Code)
	}
	if hist := f.do("GET", "/admin/aliases/chat-default/history", admin, ""); !strings.Contains(hist.Body.String(), `"version":2`) {
		t.Fatalf("history: %s", hist.Body)
	}
	if !strings.Contains(strings.Join(f.rec.Types(), ","), "gw.alias.changed") {
		t.Fatal("alias event")
	}
}

func TestComputePolicyAutoDifficultyAndReasoningDowngrade(t *testing.T) {
	f := blSetup(t)
	if w := f.do("PUT", "/admin/compute-policies/agents", admin, `{"match":{"agent":"AL-RUN"},"effort":"auto","reasoning_budget_tokens":1000}`); w.Code != 200 {
		t.Fatalf("put = %d %s", w.Code, w.Body)
	}
	if w := f.do("PUT", "/admin/compute-policies/bad", admin, `{"effort":"turbo"}`); w.Code != 400 {
		t.Fatalf("bad effort = %d", w.Code)
	}
	ask := func(text string) map[string]string {
		w := f.do("POST", "/v1/chat/completions", f.key, `{"model":"reasoner","messages":[{"role":"user","content":"`+text+`"}]}`, "x-atlas-tags", "unit_id:AL-RUN")
		if w.Code != 200 {
			t.Fatalf("chat = %d %s", w.Code, w.Body)
		}
		h := map[string]string{}
		for k := range w.Header() {
			h[strings.ToLower(k)] = w.Header().Get(k)
		}
		return h
	}
	easy := ask("이 문장을 영어로 번역해줘")
	hard := ask("이 알고리즘이 왜 O(n log n) 인지 단계별로 증명해줘")
	if easy["x-atlas-difficulty"] != "easy" || easy["x-atlas-effort"] != "low" || hard["x-atlas-difficulty"] != "hard" || hard["x-atlas-effort"] != "high" ||
		easy["x-atlas-compute-policy"] != "agents" {
		t.Fatalf("auto: easy=%v hard=%v", easy, hard)
	}
	if hard["x-atlas-reasoning-tokens"] == "" {
		t.Fatalf("reasoning tokens header missing: %v", hard)
	}
	// 추론 예산 80% 이상 → 한 단계 하향 + 이벤트 1회
	for i := 0; i < 20; i++ {
		if h := ask("단계별로 증명해줘 왜 알고리즘이"); h["x-atlas-compute-downgraded"] != "" {
			if h["x-atlas-effort"] != "medium" || h["x-atlas-compute-downgraded"] != "high->medium" {
				t.Fatalf("downgrade: %v", h)
			}
			if !strings.Contains(strings.Join(f.rec.Types(), ","), "gw.compute.downgraded") {
				t.Fatal("downgrade event")
			}
			var u map[string]any
			_ = json.Unmarshal(f.do("GET", "/admin/compute-policies/agents/usage", admin, "").Body.Bytes(), &u)
			if u["pct"].(float64) < 80 {
				t.Fatalf("usage: %v", u)
			}
			return
		}
	}
	t.Fatal("never downgraded")
}

func TestEmbeddingsMockDeterministicAndSimilar(t *testing.T) {
	f := blSetup(t)
	w := f.do("POST", "/v1/embeddings", f.key, `{"model":"embed-default","input":["환불 규정은 7일","환불 규정 안내 7일","배송 추적 방법"]}`)
	if w.Code != 200 || w.Header().Get("x-atlas-cost-usd") == "" {
		t.Fatalf("embeddings = %d %s", w.Code, w.Body)
	}
	var out struct {
		Data []struct {
			Embedding []float64 `json:"embedding"`
		} `json:"data"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	cos := func(a, b []float64) float64 {
		s := 0.0
		for i := range a {
			s += a[i] * b[i]
		}
		return s
	}
	if len(out.Data) != 3 || len(out.Data[0].Embedding) != 256 || cos(out.Data[0].Embedding, out.Data[1].Embedding) <= cos(out.Data[0].Embedding, out.Data[2].Embedding) {
		t.Fatalf("similarity: %d", len(out.Data))
	}
	w2 := f.do("POST", "/v1/embeddings", f.key, `{"model":"embed-default","input":"환불 규정은 7일"}`)
	var o2 struct {
		Data []struct {
			Embedding []float64 `json:"embedding"`
		} `json:"data"`
	}
	_ = json.Unmarshal(w2.Body.Bytes(), &o2)
	if o2.Data[0].Embedding[3] != out.Data[0].Embedding[3] {
		t.Fatal("not deterministic")
	}
	if w := f.do("POST", "/v1/embeddings", f.key, `{"model":"chat-default","input":"x"}`); w.Code != 400 {
		t.Fatalf("chat alias embedding = %d", w.Code)
	}
}
