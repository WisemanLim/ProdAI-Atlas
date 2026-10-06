# 시험 시나리오 — impl 4th · Epic 4 OB-TRACE

근거: PRD FR-OB-TRACE-01(OTLP 수신)·04(컨텍스트 전파)·05(SDK 자동 계측)·06(게이트웨이 span 결합), §17.3 관측 계약(gen_ai.* + atlas.*).

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E4-01 | 단위 시험 | `make test APP_SVC=gateway` · `make test APP_SVC=agent-al-plan` | Gateway: traceparent 이어받기·GenAI 속성 / SDK: invoke_agent 부모·inject | P0 |
| E4-02 | Collector 기동 | `otel-collector` 컨테이너 | 4317·4318 수신, file 싱크 생성 | P0 |
| E4-03 | 외부 trace 이어받기 | A2A SendMessage + `traceparent`(고정 trace_id) | 수집된 `invoke_agent al-plan` span 의 trace_id·parent 가 입력과 동일 | P0 |
| E4-04 | 에이전트 → Gateway 전파 | 같은 요청 | `chat planner-default` span(service atlas-gateway) 의 parent = invoke_agent span | P0 |
| E4-05 | GenAI 속성 | gateway span | gen_ai.operation.name=chat · gen_ai.provider.name · gen_ai.request/response.model · gen_ai.usage.input/output_tokens · atlas.cost.usd · atlas.unit_id=AL-PLAN | P0 |
| E4-06 | trace_id 연계 | Gateway 응답 헤더·`gw.request.completed`·감사 | `x-atlas-trace-id` = trace_id, 이벤트 tags.trace = trace_id | P1 |
| E4-07 | prod 리허설 | `make prod-all` → E4-03~05 | 동일 | P0 |
| E4-08 | 회귀 | `make test-all` | exit 0 | P0 |
