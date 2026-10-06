// Package catalog 는 PRD §3.3 단위 에이전트 카탈로그(47종)를 담는다. 매니페스트 metadata.unitId 검증의 기준이다.
package catalog

// Unit 은 원본 도식의 세부 항목 1개 = 마이크로 에이전트 1개다.
type Unit struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Layer   string `json:"layer"`   // L1 · L2 · L3 · band
	Concept string `json:"concept"` // 도식 카드 (01-agentic-loops …)
	Skill   string `json:"skill"`   // 기본 A2A skill id
}

var units = []Unit{
	{"AL-PLAN", "Planner", "L1", "01-agentic-loops", "loop.plan"},
	{"AL-ACT", "Actor", "L1", "01-agentic-loops", "loop.act"},
	{"AL-OBS", "Observer", "L1", "01-agentic-loops", "loop.observe"},
	{"AL-REF", "Reflector", "L1", "01-agentic-loops", "loop.reflect"},
	{"AL-EXIT", "Exit Controller", "L1", "01-agentic-loops", "loop.exit"},
	{"AL-RUN", "Loop Runtime", "L1", "01-agentic-loops", "loop.run"},
	{"MCP-HOST", "MCP Host/Client", "L1", "02-mcp", "mcp.host"},
	{"MCP-REG", "MCP Server Registry", "L1", "02-mcp", "mcp.registry"},
	{"MCP-SEL", "Tool Relevance Selector", "L1", "02-mcp", "mcp.select"},
	{"MCP-TOOL", "Tools Connector", "L1", "02-mcp", "mcp.tools"},
	{"MCP-RES", "Resources Connector", "L1", "02-mcp", "mcp.resources"},
	{"MCP-SYS", "Systems Connector", "L1", "02-mcp", "mcp.systems"},
	{"A2A-CARD", "Agent Card Registry", "L1", "a2a", "a2a.card"},
	{"A2A-SRV", "A2A Server Adapter", "L1", "a2a", "a2a.server"},
	{"A2A-CLI", "A2A Client / Delegator", "L1", "a2a", "a2a.client"},
	{"MA-ORCH", "Orchestrator", "L1", "03-multi-agent", "ma.orchestrate"},
	{"MA-RES", "Subagent A · Research", "L1", "03-multi-agent", "ma.research"},
	{"MA-CR", "Subagent B · Code Review", "L1", "03-multi-agent", "ma.code_review"},
	{"MA-ANL", "Subagent C · Analysis", "L1", "03-multi-agent", "ma.analysis"},
	{"MA-MERGE", "Merged Result", "L1", "03-multi-agent", "ma.merge"},
	{"GW-ROUTE", "Routing", "L2", "04-ai-gateway", "gw.route"},
	{"GW-AUTH", "Auth", "L2", "04-ai-gateway", "gw.auth"},
	{"GW-RATE", "Rate Limits", "L2", "04-ai-gateway", "gw.rate"},
	{"GW-KEY", "Key Rotation", "L2", "04-ai-gateway", "gw.key"},
	{"GW-COST", "Cost Tracking", "L2", "04-ai-gateway", "gw.cost"},
	{"GW-POL", "Policy & Logs", "L2", "04-ai-gateway", "gw.policy"},
	{"EC-CACHE", "Prompt Caching", "L2", "05-inference-economics", "ec.cache"},
	{"EC-BATCH", "Batching", "L2", "05-inference-economics", "ec.batch"},
	{"EC-ROUTE", "Economic Routing", "L2", "05-inference-economics", "ec.route"},
	{"EC-MODEL", "Model Selection", "L2", "05-inference-economics", "ec.model"},
	{"EC-CTRL", "Cost Control", "L2", "05-inference-economics", "ec.control"},
	{"GR-IO", "Input/Output Safety", "L2", "07-guardrails", "gr.io"},
	{"GR-POL", "Policy Enforcement", "L2", "07-guardrails", "gr.policy"},
	{"GR-JB", "Jailbreak Defense", "L2", "07-guardrails", "gr.jailbreak"},
	{"GR-PII", "PII Redaction", "L2", "07-guardrails", "gr.pii"},
	{"EV-GOLD", "Golden Datasets", "L3", "06-evals", "ev.golden"},
	{"EV-REG", "Regression Suites", "L3", "06-evals", "ev.regression"},
	{"EV-JUDGE", "LLM-as-Judge", "L3", "06-evals", "ev.judge"},
	{"EV-SCORE", "Score & Trend", "L3", "06-evals", "ev.score"},
	{"OB-TRACE", "Traces", "L3", "08-observability", "ob.trace"},
	{"OB-LOG", "Logs", "L3", "08-observability", "ob.log"},
	{"OB-DEBUG", "Debug", "L3", "08-observability", "ob.debug"},
	{"OB-DASH", "Dashboards & Alerts", "L3", "08-observability", "ob.dash"},
	{"BL-MODEL", "Model Swap", "L3", "09-bitter-lesson", "bl.model"},
	{"BL-HARN", "Harness Config", "L3", "09-bitter-lesson", "bl.harness"},
	{"BL-COMP", "Compute Policy", "L3", "09-bitter-lesson", "bl.compute"},
	{"MAT", "Maturity Assessment", "band", "maturity", "mat.assess"},
}

// platform 은 도식 밖 교차 기능 단위(PRD §12 X-*)다 — 47종 지도에는 없고 매니페스트 검증·등록에만 쓴다.
var platform = []Unit{
	{"X-KB", "Knowledge & Memory", "X", "x-platform", "kb.search"},
	{"X-SBX", "Code Sandbox", "X", "x-platform", "sbx.run_code"},
	{"X-MKT", "Marketplace", "X", "x-platform", "mkt.install"},
	{"X-RPT", "Reporting & Audit", "X", "x-platform", "rpt.usage"},
}

// Platform 은 교차 기능 단위 목록(복사본)이다.
func Platform() []Unit { return append([]Unit(nil), platform...) }

// All 은 카탈로그 전체(복사본)를 돌려준다.
func All() []Unit { return append([]Unit(nil), units...) }

// Get 은 unitId 로 단위를 찾는다.
func Get(id string) (Unit, bool) {
	for _, u := range append(units, platform...) {
		if u.ID == id {
			return u, true
		}
	}
	return Unit{}, false
}
