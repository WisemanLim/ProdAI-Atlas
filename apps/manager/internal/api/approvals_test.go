package api

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"atlas-manager/internal/notify"
	"atlas-manager/internal/store"
)

func TestApprovalInboxDecideAndResume(t *testing.T) {
	f := newAuthServer(t)
	var mu sync.Mutex
	var bodies []string
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		bodies = append(bodies, string(b))
		mu.Unlock()
		_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{"task":{"status":{"state":"TASK_STATE_WORKING"}}}}`))
	}))
	defer agent.Close()
	id := readyAgent(t, f, agent.URL)
	mk := func(risk, ref string, ttl int) map[string]any {
		w, a := f.do("POST", "/api/v1/approvals", f.key["acme-dev"], `{"source":"loop","sourceRef":"`+ref+`","agentId":"`+id+`","action":"payments.refund","riskLevel":"`+risk+`","payload":{"tool":"payments.refund","arguments":{"amount":500}},"expiresInS":`+itoa(ttl)+`}`)
		if w.Code != 201 {
			t.Fatalf("create = %d %s", w.Code, w.Body)
		}
		return a
	}
	low := mk("low", "t-low", 600)
	crit := mk("critical", "t-crit", 3600)
	high1 := mk("high", "t-h1", 3600)
	high2 := mk("high", "t-h2", 120)
	_, l := f.do("GET", "/api/v1/approvals?org=acme&status=pending", f.key["acme-ro"], "")
	var order []string
	for _, it := range l["items"].([]any) {
		order = append(order, it.(map[string]any)["id"].(string))
	}
	want := []string{crit["id"].(string), high2["id"].(string), high1["id"].(string), low["id"].(string)}
	if strings.Join(order, ",") != strings.Join(want, ",") {
		t.Fatalf("sort risk→expiry: %v want %v", order, want)
	}
	// 다른 조직은 보지 못한다
	if _, b := f.do("GET", "/api/v1/approvals?org=beta", f.key["beta-dev"], ""); len(b["items"].([]any)) != 0 {
		t.Fatalf("cross-tenant: %v", b)
	}
	// 읽기 전용 키는 결정 불가
	if w, _ := f.do("POST", "/api/v1/approvals/"+crit["id"].(string)+"/decision", f.key["acme-ro"], `{"decision":"approve"}`); w.Code != 403 {
		t.Fatalf("ro decide = %d", w.Code)
	}
	// 수정 후 승인 → 같은 Task 에 approval=approve + edited_arguments
	w, d := f.do("POST", "/api/v1/approvals/"+crit["id"].(string)+"/decision", f.key["acme-admin"], `{"decision":"edit","reason":"금액 축소","payload":{"arguments":{"amount":100}}}`)
	if w.Code != 200 || d["status"] != "edited" || d["callbackResult"] != "TASK_STATE_WORKING" {
		t.Fatalf("edit = %d %v", w.Code, d)
	}
	mu.Lock()
	last := bodies[len(bodies)-1]
	mu.Unlock()
	var rpc struct {
		Method string `json:"method"`
		Params struct {
			Message struct {
				TaskID string `json:"taskId"`
				Parts  []struct {
					Data map[string]any `json:"data"`
				} `json:"parts"`
			} `json:"message"`
		} `json:"params"`
	}
	_ = json.Unmarshal([]byte(last), &rpc)
	data := rpc.Params.Message.Parts[0].Data
	if rpc.Method != "SendMessage" || rpc.Params.Message.TaskID != "t-crit" || data["approval"] != "approve" || data["edited_arguments"].(map[string]any)["amount"].(float64) != 100 {
		t.Fatalf("callback: %s", last)
	}
	// 두 번째 결정은 409
	if w, _ := f.do("POST", "/api/v1/approvals/"+crit["id"].(string)+"/decision", f.key["acme-admin"], `{"decision":"reject"}`); w.Code != 409 {
		t.Fatalf("double decide = %d", w.Code)
	}
	// 반려 사유 → 골든셋 후보 이벤트
	f.do("POST", "/api/v1/approvals/"+high1["id"].(string)+"/decision", f.key["acme-admin"], `{"decision":"reject","reason":"환불 정책 위반"}`)
	if ty := strings.Join(f.rec.Types(), ","); !strings.Contains(ty, "plt.approval.rejected") || !strings.Contains(ty, "plt.approval.decided") {
		t.Fatalf("events: %s", ty)
	}
	// 감사: 결정 + payload 해시
	_, au := f.do("GET", "/api/v1/orgs/acme/audit?action=approval.decide", f.key["acme-admin"], "")
	if items := au["items"].([]any); len(items) != 2 {
		t.Fatalf("audit decide: %v", au)
	}
	// 만료 → expired + deny 콜백
	f.s.DB.Model(&store.Approval{}).Where("id = ?", low["id"]).Update("expires_at", time.Now().UTC().Add(-time.Minute))
	f.s.ExpireApprovals(t.Context())
	var ap store.Approval
	f.s.DB.First(&ap, "id = ?", low["id"])
	mu.Lock()
	last = bodies[len(bodies)-1]
	mu.Unlock()
	if ap.Status != "expired" || !strings.Contains(last, `"approval":"deny"`) || !strings.Contains(last, "t-low") {
		t.Fatalf("expire: %s %s", ap.Status, last)
	}
	// 같은 Task 에서 이미 재개 → 요청 에이전트가 거둔다 (다른 에이전트 토큰은 403)
	atok, _ := f.s.Signer.Mint("agent:"+id, agentAudience, "acme", "default", "dev", time.Minute)
	other, _ := f.s.Signer.Mint("agent:someone", agentAudience, "acme", "default", "dev", time.Minute)
	if w, _ := f.do("POST", "/api/v1/approvals/withdraw?org=acme", other, `{"sourceRef":"t-h2","agentId":"`+id+`"}`); w.Code != 403 {
		t.Fatalf("other agent withdraw = %d", w.Code)
	}
	if w, out := f.do("POST", "/api/v1/approvals/withdraw?org=acme", atok, `{"sourceRef":"t-h2","agentId":"`+id+`"}`); w.Code != 200 || len(out["withdrawn"].([]any)) != 1 {
		t.Fatalf("withdraw = %d %v", w.Code, out)
	}
	if w, _ := f.do("POST", "/api/v1/approvals/"+high2["id"].(string)+"/decision", f.key["acme-admin"], `{"decision":"approve"}`); w.Code != 409 {
		t.Fatalf("decide withdrawn = %d", w.Code)
	}
	// 감사 검증: 같은 조직 에이전트는 허용(결과만), 다른 조직 에이전트·감사 목록은 거부
	if w, _ := f.do("GET", "/api/v1/orgs/acme/audit/verify", atok, ""); w.Code != 200 {
		t.Fatalf("agent verify = %d", w.Code)
	}
	if w, _ := f.do("GET", "/api/v1/orgs/beta/audit/verify", atok, ""); w.Code != 403 {
		t.Fatalf("cross-org agent verify = %d", w.Code)
	}
	if w, _ := f.do("GET", "/api/v1/orgs/acme/audit", atok, ""); w.Code != 403 {
		t.Fatalf("agent audit list = %d", w.Code)
	}
	if w, _ := f.do("POST", "/api/v1/approvals/"+high2["id"].(string)+"/decision", f.key["acme-admin"], `{"decision":"maybe"}`); w.Code != 400 {
		t.Fatalf("bad decision = %d", w.Code)
	}
}

func itoa(n int) string { b, _ := json.Marshal(n); return string(b) }

func TestNotificationRulesAPI(t *testing.T) {
	f := newAuthServer(t)
	f.s.Notify = &notify.Engine{DB: f.s.DB, HTTP: http.DefaultClient, AllowHosts: notify.Hosts("hooks.test"), Now: time.Now}
	body := `{"name":"budget","topic":"gw.budget.threshold","filter":[{"field":"threshold","op":">=","value":80}],"channels":[{"type":"webhook","url":"http://hooks.test/x","secret":"k1"}],"throttleS":30}`
	if w, _ := f.do("POST", "/api/v1/orgs/acme/notifications/rules", f.key["acme-dev"], body); w.Code != 403 {
		t.Fatalf("non-admin create = %d", w.Code)
	}
	w, r := f.do("POST", "/api/v1/orgs/acme/notifications/rules", f.key["acme-admin"], body)
	if w.Code != 201 || r["channels"].([]any)[0].(map[string]any)["secret"] != "***" || r["enabled"] != true {
		t.Fatalf("create = %d %v", w.Code, r)
	}
	id := r["id"].(string)
	if w, out := f.do("POST", "/api/v1/orgs/acme/notifications/rules", f.key["acme-admin"], strings.Replace(body, "http://hooks.test/x", "http://169.254.169.254/latest", 1)); w.Code != 400 || out["error"] != "unsafe_url" {
		t.Fatalf("ssrf = %d %v", w.Code, out)
	}
	// "***" 는 기존 키 유지
	upd := strings.Replace(strings.Replace(body, `"k1"`, `"***"`, 1), `"throttleS":30`, `"throttleS":60,"enabled":false`, 1)
	if w, _ := f.do("PUT", "/api/v1/orgs/acme/notifications/rules/"+id, f.key["acme-admin"], upd); w.Code != 200 {
		t.Fatalf("update = %d", w.Code)
	}
	var row store.NotificationRule
	f.s.DB.First(&row, "id = ?", id)
	if !strings.Contains(row.Channels, `"secret":"k1"`) || row.Enabled || row.ThrottleS != 60 {
		t.Fatalf("stored: %+v", row)
	}
	if _, l := f.do("GET", "/api/v1/orgs/beta/notifications/rules", f.key["beta-dev"], ""); len(l["items"].([]any)) != 0 {
		t.Fatalf("cross-tenant rules: %v", l)
	}
	if _, tp := f.do("GET", "/api/v1/notifications/templates", f.key["acme-ro"], ""); len(tp["items"].([]any)) < 4 {
		t.Fatalf("templates: %v", tp)
	}
	if w, _ := f.do("DELETE", "/api/v1/orgs/acme/notifications/rules/"+id, f.key["acme-admin"], ""); w.Code != 204 {
		t.Fatalf("delete = %d", w.Code)
	}
}
