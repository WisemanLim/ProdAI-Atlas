# 시험 시나리오 — impl 9th · Epic 9 Observability

근거: PRD FR-OB-TRACE-03·07 · OB-LOG-01·02·03·06 · OB-DEBUG-01 · OB-DASH-01·03·04·05, §14.5 저장소(trace 30일).

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E9-01 | 단위 시험 | `make test-all` | gateway(capture·비밀 제거) · agent pytest(OB 14 포함) | P0 |
| E9-02 | 저장소 | ClickHouse `otel.otel_traces` | 테이블·TTL 30일, span 유입, 프로브 경로 span 없음 | P0 |
| E9-03 | 검색 DSL | `GET /v1/traces?q=gen_ai.request.model = "agent-default" AND trace_id = "<t>"` | 해당 trace 1건, cost·models·services | P0 |
| E9-04 | span 트리 | `GET /v1/traces/{t}` | roots·waterfall·위임 그래프(al-run→gateway·al-plan) | P0 |
| E9-05 | 세션 | `GET /v1/sessions/{contextId}` | 같은 contextId 의 trace | P1 |
| E9-06 | 본문 기록·비밀 차단 | 전화번호+AKIA 키 포함 요청 | span 속성 본문에 마스킹·[SECRET], `gw.secret_leak_detected` 이벤트 | P0 |
| E9-07 | 결정 이벤트 | 가드·루프 실행 | span 이벤트 atlas.guardrail.decision·atlas.loop.exit 저장 | P0 |
| E9-08 | 재실행 | `POST /v1/replay {trace_id, model}` | 원본·재실행 출력 나란히 | P0 |
| E9-09 | 대시보드·비용 | `/v1/dashboards/llm?group_by=model` · `/v1/costs?group_by=agent` | requests·p95·tokens·cost, 에이전트별 비용 | P0 |
| E9-10 | SLO·경보 | latency SLO(임계 매우 낮게) + slo_burn 규칙 → evaluate | burn_rate > 1, firing 1회·웹훅 수신(local echo)·trace 링크 | P0 |
| E9-11 | prod 리허설 | `make prod-all` → E9-02~10(웹훅 제외) | 동일 | P0 |
| E9-12 | 회귀 | `make test-all` | exit 0 | P0 |
