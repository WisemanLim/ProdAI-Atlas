package provider

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"unicode"
)

// mockJudge 는 LLM-as-Judge 시험용 결정적 판정기다 (FR-EV-JUDGE-01~04·08). 프롬프트의 "## 섹션" 을 읽는다:
//
//	## MODE binary|pairwise|claim · ## RUBRIC · ## OUTPUT | ## OUTPUT A + ## OUTPUT B | ## CLAIM + ## CONTEXT
//
// 루브릭 줄 "포함: a, b" / "include: …" 는 필수 표현, "금지: …" / "exclude: …" 는 금지 표현이다.
// 모델 변형: mock-judge(엄격: 필수 전부) · mock-judge-lenient(필수 하나 이상) · mock-judge-length(20자 이상) · mock-judge-biased(항상 A·pass).
var (
	judgeSection = regexp.MustCompile(`(?m)^## ([A-Z][A-Z ]*)\s*$`)
	judgeRule    = regexp.MustCompile(`(?im)^\s*[-*]?\s*(포함|include|must include|금지|exclude|must not)\s*[:：]\s*(.+)$`)
)

func judgeSections(text string) map[string]string {
	out := map[string]string{}
	idx := judgeSection.FindAllStringSubmatchIndex(text, -1)
	for i, m := range idx {
		end := len(text)
		if i+1 < len(idx) {
			end = idx[i+1][0]
		}
		out[strings.TrimSpace(text[m[2]:m[3]])] = strings.TrimSpace(text[m[1]:end])
	}
	return out
}

func judgeRules(rubric string) (must, forbid []string) {
	for _, m := range judgeRule.FindAllStringSubmatch(rubric, -1) {
		var list []string
		for _, t := range strings.Split(m[2], ",") {
			if t = strings.TrimSpace(t); t != "" {
				list = append(list, strings.ToLower(t))
			}
		}
		switch strings.ToLower(m[1]) {
		case "금지", "exclude", "must not":
			forbid = append(forbid, list...)
		default:
			must = append(must, list...)
		}
	}
	return
}

func hits(text string, words []string) int {
	t, n := strings.ToLower(text), 0
	for _, w := range words {
		if strings.Contains(t, w) {
			n++
		}
	}
	return n
}

func judgeTokens(s string) []string {
	var out []string
	for _, f := range strings.FieldsFunc(strings.ToLower(s), func(r rune) bool { return !unicode.IsLetter(r) && !unicode.IsDigit(r) }) {
		if len([]rune(f)) >= 2 {
			out = append(out, f)
		}
	}
	return out
}

func mockJudge(req Request) Response {
	var prompt strings.Builder
	for _, m := range req.Messages {
		prompt.WriteString(m.Content + "\n")
	}
	sec := judgeSections(prompt.String())
	must, forbid := judgeRules(sec["RUBRIC"])
	variant := strings.TrimPrefix(strings.TrimPrefix(req.Model, "mock-judge"), "-")
	var out map[string]any
	switch strings.ToLower(sec["MODE"]) {
	case "pairwise":
		a, b := sec["OUTPUT A"], sec["OUTPUT B"]
		sa, sb := hits(a, must)-2*hits(a, forbid), hits(b, must)-2*hits(b, forbid)
		w := "tie"
		switch {
		case variant == "biased":
			w = "A"
		case sa > sb:
			w = "A"
		case sb > sa:
			w = "B"
		}
		out = map[string]any{"winner": w, "critique": fmt.Sprintf("A 필수 %d/금지 %d · B 필수 %d/금지 %d", hits(a, must), hits(a, forbid), hits(b, must), hits(b, forbid))}
	case "claim":
		claim, ctx := judgeTokens(sec["CLAIM"]), strings.ToLower(sec["CONTEXT"])
		in := 0
		for _, t := range claim {
			if strings.Contains(ctx, t) {
				in++
			}
		}
		ratio := 0.0
		if len(claim) > 0 {
			ratio = float64(in) / float64(len(claim))
		}
		out = map[string]any{"supported": ratio >= 0.6, "score": ratio, "critique": fmt.Sprintf("주장 토큰 %d/%d 문맥에 있음", in, len(claim))}
	default:
		o := sec["OUTPUT"]
		h, f := hits(o, must), hits(o, forbid)
		pass := f == 0
		switch variant {
		case "lenient":
			pass = pass && (len(must) == 0 || h > 0)
		case "length":
			pass = pass && len([]rune(o)) >= 20
		case "biased":
			pass = true
		default:
			pass = pass && h == len(must)
		}
		score := 1.0
		if len(must) > 0 {
			score = float64(h) / float64(len(must))
		}
		if f > 0 {
			score = 0
		}
		verdict := map[bool]string{true: "pass", false: "fail"}[pass]
		out = map[string]any{"verdict": verdict, "score": score,
			"critique": fmt.Sprintf("필수 표현 %d/%d 충족, 금지 표현 %d건 (%s)", h, len(must), f, req.Model)}
	}
	b, _ := json.Marshal(out)
	return Response{Content: string(b), FinishReason: "stop",
		Usage: Usage{Prompt: EstimateTokens(PromptText(req.Messages)), Completion: EstimateTokens(string(b))}}
}
