# 시험 시나리오 — impl 7th · Epic 7 MCP 핵심

근거: PRD FR-MCP-HOST-01·02·06·07·09 · MCP-REG-01·03·05·06 · MCP-SEL-01·02·03 · MCP-TOOL-01·03, OWASP MCP03(tool poisoning)·MCP09(Shadow MCP).

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E7-01 | 단위 시험 | `make test-all` | agent pytest(MCP 10 포함) 통과 | P0 |
| E7-02 | 등록·스캔·승인 | `make mcp-seed` | demo·manager-api 둘 다 scanned → approved, pins 4·6 | P0 |
| E7-03 | 레지스트리 API v0.1 | `GET /v0.1/servers?search=` · `/versions/{v}` | servers[].server + _meta, pins | P0 |
| E7-04 | 허용목록 | `/v0.1/tenants/{t}/effective` | default 2개 · 허용목록 없는 테넌트 0개(Shadow MCP 차단) | P0 |
| E7-05 | 루프에서 MCP 도구 | loop.run `demo__search_docs` | 출력에 문서 내용, 이벤트 mcp·tools_selected(선별) | P0 |
| E7-06 | OpenAPI→MCP | loop.run `manager_api__listCatalogUnits` | 출력에 카탈로그 단위(AL-PLAN) | P0 |
| E7-07 | 승인 규칙 | loop.run `demo__delete_records` | INPUT_REQUIRED(destructiveHint, 신뢰 서버) | P0 |
| E7-08 | rug pull | 승인 후 mcp-demo 를 DEMO_POISON=true 로 재기동 → loop.run | 이벤트 blocked 에 demo__search_docs(pin_mismatch), 도구 미노출 | P0 |
| E7-09 | 설치 전 스캔 | 오염 서버 등록 | rejected, score ≥ 70 | P0 |
| E7-10 | 감사 trace | E7-05 + traceparent | `mcp.tools.call demo__search_docs` span 이 invoke_agent al-run 하위, gen_ai.tool.name | P0 |
| E7-11 | prod 리허설 | `make prod-all` → mcp-seed → E7-02~07 | 동일 | P0 |
| E7-12 | 회귀 | `make test-all` | exit 0 | P0 |
