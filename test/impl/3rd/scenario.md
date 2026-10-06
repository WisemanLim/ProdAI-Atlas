# 시험 시나리오 — impl 3rd · Epic 3 AI Gateway (GW-* 6종)

근거: PRD FR-GW-ROUTE-01·03·05·07·08 · AUTH-01·05 · RATE-01·02 · KEY-01·06 · COST-01·03·04 · POL-01·04·05, §8.7 요청 파이프라인, §17.3, NFR-PERF-01, COMPLIANCE CMP-02.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E3-01 | 단위 시험 | `make test APP_SVC=gateway` | cost(정밀·버전)·audit(변조 탐지)·router(재시도·폴백·서킷)·provider(키 풀·OpenAI 호환)·server(파이프라인·OpenAPI 정합) ok | P0 |
| E3-02 | 헬스 | `GET :8090/health` | 200, redis ok | P0 |
| E3-03 | 인증 실패 | 키 없이 chat | 401 `invalid_api_key` | P0 |
| E3-04 | 가상키 발급·호출 | `POST /admin/keys` → chat(chat-default) | 키 형식 `sk-atlas-local-<22>`, 200, `x-atlas-cost-usd`(8자리)·`x-atlas-route` | P0 |
| E3-05 | 모델 허용 목록 | chat-* 키로 planner-default | 403 `model_not_allowed` | P0 |
| E3-06 | RPM 한도 | rpm=3 키로 4회 | 4번째 429 `rate_limit_exceeded` + Retry-After | P0 |
| E3-07 | 폴백 | flaky-chat | 200, `x-atlas-fallback: chat-default` | P0 |
| E3-08 | 스트리밍 | stream=true | SSE 청크 합 = 응답, 마지막 usage, `[DONE]` | P0 |
| E3-09 | 비용 집계 | `GET /admin/usage` | requests·tokens·cost_usd 누적 | P0 |
| E3-10 | 이벤트 | NATS 스트림 `GW` | 완료 요청 수만큼 `gw.request.completed` 증가 | P0 |
| E3-11 | 감사 해시 체인 | gateway 로그 → `-verify-audit` | `audit chain ok` | P0 |
| E3-12 | AL-PLAN → Gateway | A2A SendMessage | plan.source=`gateway:planner-default`, 부트스트랩 키 사용량 증가 | P0 |
| E3-13 | 키 폐기 | `DELETE /admin/keys/{prefix}` → chat | 401 | P0 |
| E3-14 | 추가 지연 | mock(0ms) 200회 순차 | p95 측정·기록 (목표 ≤ 15ms, NFR-PERF-01 은 4vCPU·500RPS 조건 — 참고치) | P1 |
| E3-15 | prod 리허설 | `make prod-all`(셸 env 로 키 주입) → E3-02~13 | 동일 | P0 |
| E3-16 | 회귀 | `make test-all` | exit 0 | P0 |
