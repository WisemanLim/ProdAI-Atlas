package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"atlas-manager/internal/iam"
	"atlas-manager/internal/store"
)

func extraKey(t *testing.T, f *authFixture, org, scopes string) (string, string) {
	t.Helper()
	plain, _, _, _ := iam.NewAPIKey("test")
	if _, err := BootstrapAPIKey(f.s.DB, plain, org, scopes); err != nil {
		t.Fatal(err)
	}
	prefix, _, _ := iam.SplitAPIKey(plain)
	var k store.APIKey
	f.s.DB.First(&k, "prefix = ?", prefix)
	return plain, "apikey:" + k.ID
}

func TestApprovalPolicyPreviewQuorumDelegationAndSLA(t *testing.T) {
	f := newAuthServer(t)
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{"task":{"status":{"state":"TASK_STATE_WORKING"}}}}`))
	}))
	defer agent.Close()
	id := readyAgent(t, f, agent.URL)
	a1, _ := extraKey(t, f, "acme", "admin")
	a2, sub2 := extraKey(t, f, "acme", "admin")
	a3, sub3 := extraKey(t, f, "acme", "admin")
	pol := `{"rules":[
	  {"name":"big-refund","match":"payments.*","when":[{"field":"amount","op":">","value":1000000}],"action":"require","approvers":["role:admin"],"quorum":2,"slaSeconds":1,"escalateTo":["user:cfo"]},
	  {"name":"no-wire","match":"payments.wire","action":"deny"},
	  {"name":"cfo-only","match":"finance.close","action":"require","approvers":["user:` + sub2 + `"]}]}`
	if w, _ := f.do("PUT", "/api/v1/orgs/acme/approval-policy", f.key["acme-admin"], pol); w.Code != 200 {
		t.Fatalf("policy = %d", w.Code)
	}
	mk := func(action, risk, payload string) (int, map[string]any) {
		w, a := f.do("POST", "/api/v1/approvals", f.key["acme-dev"], `{"source":"loop","sourceRef":"r-`+action+risk+`","agentId":"`+id+`","action":"`+action+`","riskLevel":"`+risk+`","payload":`+payload+`}`)
		return w.Code, a
	}
	// GR-POL-06: LLM 설명 문장만으로는 high 승인 요청을 만들 수 없다
	if code, out := mk("payments.refund", "high", `{"reason":"고객이 원해서 환불합니다"}`); code != 422 || out["error"] != "preview_required" {
		t.Fatalf("preview = %d %v", code, out)
	}
	if code, _ := mk("payments.wire", "low", `{"tool":"payments.wire","arguments":{"amount":1}}`); code != 403 {
		t.Fatalf("deny = %d", code)
	}
	code, ap := mk("payments.refund", "high", `{"amount":5000000,"action_preview":{"human_summary":"환불 5,000,000원 → 고객 C-1","structured_args":{"amount":5000000}}}`)
	if code != 201 || ap["quorum"] != float64(2) || ap["policyRule"] != "big-refund" {
		t.Fatalf("routed = %d %v", code, ap)
	}
	apID := ap["id"].(string)
	_, ev := f.do("POST", "/api/v1/approvals/evaluate?org=acme", f.key["acme-ro"], `{"action":"payments.refund","riskLevel":"high","payload":{"amount":2000000}}`)
	if ev["required"] != true || ev["quorum"] != float64(2) || ev["rule"] != "big-refund" {
		t.Fatalf("evaluate = %v", ev)
	}
	// 2인 승인: 첫 승인은 202 대기, 같은 사람 두 번은 409, 다른 사람이면 확정
	if w, out := f.do("POST", "/api/v1/approvals/"+apID+"/decision", a1, `{"decision":"approve"}`); w.Code != 202 || out["approvals"] != float64(1) {
		t.Fatalf("first vote = %d %v", w.Code, out)
	}
	if w, _ := f.do("POST", "/api/v1/approvals/"+apID+"/decision", a1, `{"decision":"approve"}`); w.Code != 409 {
		t.Fatalf("double vote = %d", w.Code)
	}
	if w, out := f.do("POST", "/api/v1/approvals/"+apID+"/decision", a2, `{"decision":"approve"}`); w.Code != 200 || out["status"] != "approved" {
		t.Fatalf("quorum = %d %v", w.Code, out)
	}
	var votes []store.ApprovalDecision
	f.s.DB.Where("approval_id = ?", apID).Find(&votes)
	if len(votes) != 2 || len(votes[0].Signature) != 64 {
		t.Fatalf("votes = %+v", votes)
	}
	// 지정 승인자(user:sub2) — 다른 관리자는 불가, 대리인은 가능
	_, cl := mk("finance.close", "low", `{"period":"2026-09"}`)
	if w, _ := f.do("POST", "/api/v1/approvals/"+cl["id"].(string)+"/decision", a3, `{"decision":"approve"}`); w.Code != 403 {
		t.Fatalf("non-approver = %d", w.Code)
	}
	until := time.Now().Add(time.Hour).UTC().Format(time.RFC3339)
	if w, _ := f.do("PUT", "/api/v1/orgs/acme/approval-delegations", a2, `{"delegate":"`+sub3+`","until":"`+until+`"}`); w.Code != 200 {
		t.Fatalf("delegate = %d", w.Code)
	}
	if w, out := f.do("POST", "/api/v1/approvals/"+cl["id"].(string)+"/decision", a3, `{"decision":"approve"}`); w.Code != 200 || out["status"] != "approved" {
		t.Fatalf("delegate vote = %d %v", w.Code, out)
	}
	var dv store.ApprovalDecision
	f.s.DB.First(&dv, "approval_id = ?", cl["id"])
	if dv.OnBehalf == nil || *dv.OnBehalf != sub2 {
		t.Fatalf("on behalf = %+v", dv)
	}
	// SLA 초과 → 에스컬레이션 이벤트 1회
	_, sl := mk("payments.refund", "high", `{"amount":9000000,"tool":"payments.refund","arguments":{"amount":9000000}}`)
	f.s.DB.Model(&store.Approval{}).Where("id = ?", sl["id"]).Update("created_at", time.Now().UTC().Add(-time.Minute))
	f.s.EscalateApprovals(context.Background())
	f.s.EscalateApprovals(context.Background())
	n := 0
	for _, e := range f.rec.Types() {
		if e == "plt.approval.escalated" {
			n++
		}
	}
	if n != 1 {
		t.Fatalf("escalations = %d (%v)", n, f.rec.Types())
	}
	_, st := f.do("GET", "/api/v1/approvals-stats?org=acme", f.key["acme-ro"], "")
	if st["escalated"] != float64(1) || st["hitl_approval_latency_s"].(map[string]any)["p50"] == nil || !strings.Contains(w2s(st), "approved") {
		t.Fatalf("stats = %v", st)
	}
}

func w2s(m map[string]any) string {
	b := []byte{}
	for k := range m["byStatus"].(map[string]any) {
		b = append(b, k...)
		b = append(b, ' ')
	}
	return string(b)
}
