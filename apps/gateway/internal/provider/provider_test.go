package provider

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestKeyPoolDisablesAfterThreeAuthFailures(t *testing.T) {
	p := NewKeyPool("openai", []string{"sk-a-1234567890", "sk-b-1234567890"})
	for range 3 {
		p.Report(0, 401)
	}
	if p.Status()[0].Status != "disabled" {
		t.Fatalf("status=%v", p.Status())
	}
	for range 4 {
		if _, i, _ := p.Pick(); i != 1 {
			t.Fatalf("picked disabled key %d", i)
		}
	}
	if err := p.Compromise(1); err != nil {
		t.Fatal(err)
	}
	if _, _, ok := p.Pick(); ok {
		t.Fatal("all keys disabled but Pick ok")
	}
}

func TestOpenAICompatibleUpstream(t *testing.T) {
	var gotAuth string
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []map[string]any{{"message": map[string]string{"role": "assistant", "content": "hi"}, "finish_reason": "stop"}},
			"usage":   map[string]int{"prompt_tokens": 7, "completion_tokens": 1},
		})
	}))
	defer up.Close()
	o := OpenAI{BaseURL: up.URL, Pool: NewKeyPool("openai", []string{"sk-upstream-1"}), Client: up.Client()}
	res, err := o.Complete(context.Background(), Request{Model: "m", Messages: []Message{{Role: "user", Content: "hi"}}})
	if err != nil || res.Content != "hi" || res.Usage.Prompt != 7 || res.Usage.Estimated || gotAuth != "Bearer sk-upstream-1" {
		t.Fatalf("res=%+v err=%v auth=%s", res, err, gotAuth)
	}
}

func TestMockPlannerReturnsJSON(t *testing.T) {
	res, err := Mock{}.Complete(context.Background(), Request{Model: "mock-planner", ResponseFormat: "json_object",
		Messages: []Message{{Role: "user", Content: "설계한다. 그리고 구현한다"}}})
	var plan struct {
		Steps []struct{ Title string } `json:"steps"`
		Dod   []any                    `json:"dod"`
	}
	if err != nil || json.Unmarshal([]byte(res.Content), &plan) != nil || len(plan.Steps) != 2 || len(plan.Dod) != 1 {
		t.Fatalf("res=%+v err=%v", res, err)
	}
}

func TestMockAgentToolCallingScript(t *testing.T) {
	tools := []json.RawMessage{
		json.RawMessage(`{"type":"function","function":{"name":"atlas_calc","parameters":{}}}`),
		json.RawMessage(`{"type":"function","function":{"name":"atlas_echo","parameters":{}}}`),
	}
	goal := Message{Role: "user", Content: `atlas_echo 로 확인하기 전에 atlas_calc({"expr":"6*7"}) 계산`}
	r, _ := Mock{}.Complete(context.Background(), Request{Model: "mock-agent", Tools: tools, Messages: []Message{goal}})
	if r.FinishReason != "tool_calls" || len(r.ToolCalls) != 2 || r.ToolCalls[0].Function.Name != "atlas_echo" ||
		r.ToolCalls[1].Function.Arguments != `{"expr":"6*7"}` {
		t.Fatalf("first turn = %+v", r)
	}
	msgs := []Message{goal, {Role: "assistant", ToolCalls: r.ToolCalls}, {Role: "tool", ToolCallID: "call_0_0", Content: "ok"}, {Role: "tool", ToolCallID: "call_0_1", Content: "42"}}
	r, _ = Mock{}.Complete(context.Background(), Request{Model: "mock-agent", Tools: tools, Messages: msgs})
	if r.FinishReason != "stop" || r.Content != "[mock:final] ok | 42" {
		t.Fatalf("final = %+v", r)
	}
	loop, _ := Mock{}.Complete(context.Background(), Request{Model: "mock-agent", Tools: tools,
		Messages: []Message{{Role: "user", Content: "repeat-forever atlas_echo"}, {Role: "tool", Content: "x"}}})
	if len(loop.ToolCalls) != 1 || loop.ToolCalls[0].Function.Arguments != "{}" {
		t.Fatalf("repeat = %+v", loop)
	}
}

func TestMockJudgeModes(t *testing.T) {
	ask := func(model, body string) map[string]any {
		r, err := Mock{}.Complete(context.Background(), Request{Model: model, Messages: []Message{{Role: "user", Content: body}}})
		if err != nil {
			t.Fatal(err)
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(r.Content), &m); err != nil {
			t.Fatalf("json: %s", r.Content)
		}
		return m
	}
	rub := "## MODE\nbinary\n## RUBRIC\n- 포함: 환불, 영업일\n- 금지: 욕설\n## OUTPUT\n"
	if v := ask("mock-judge", rub+"환불은 3 영업일 안에 처리됩니다"); v["verdict"] != "pass" || v["score"].(float64) != 1 {
		t.Fatalf("strict pass: %v", v)
	}
	if v := ask("mock-judge", rub+"환불됩니다"); v["verdict"] != "fail" {
		t.Fatalf("strict fail: %v", v)
	}
	if v := ask("mock-judge-lenient", rub+"환불됩니다"); v["verdict"] != "pass" {
		t.Fatalf("lenient: %v", v)
	}
	pw := "## MODE\npairwise\n## RUBRIC\n- 포함: 환불, 영업일\n## OUTPUT A\n모름\n## OUTPUT B\n환불은 3 영업일\n"
	if v := ask("mock-judge", pw); v["winner"] != "B" {
		t.Fatalf("pairwise: %v", v)
	}
	if v := ask("mock-judge-biased", pw); v["winner"] != "A" {
		t.Fatalf("biased: %v", v)
	}
	cl := "## MODE\nclaim\n## CLAIM\n환불 기한은 7일이다\n## CONTEXT\n고객 환불 기한은 7일이다.\n"
	if v := ask("mock-judge", cl); v["supported"] != true {
		t.Fatalf("claim: %v", v)
	}
	if v := ask("mock-judge", "## MODE\nclaim\n## CLAIM\n배송은 무료이다\n## CONTEXT\n환불 기한은 7일\n"); v["supported"] != false {
		t.Fatalf("unsupported claim: %v", v)
	}
}
