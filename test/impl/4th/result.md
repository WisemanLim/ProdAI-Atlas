# 시험 결과 / Test Result — impl 4th · Epic 4 OB-TRACE

- 일자 Date: 2026-10-02
- 대상 Target: OTel Collector(`compose/otel-collector.yml`, contrib 0.136.0) · Gateway 계측(`internal/telemetry`) · SDK 계측(`atlas_agent_core/otel.py`)
- 커밋 Commit: — (git 저장소 아님)

## 요약 / Summary

- 전체 Total: 8, 통과 Pass: 8, 실패 Fail: 0, 최종 Verdict: **PASS** (round 2 — 시험 격리 문제 1건 수정)

## 케이스 / Cases

| id | 결과 result | 비고 note |
|----|-------------|-----------|
| E4-01 | PASS (round 2) | gateway go test ok(span 부모·GenAI 속성) · SDK pytest 28 passed |
| E4-02 | PASS | Collector OTLP 수신 → file 싱크에 span 기록 |
| E4-03 | PASS | `invoke_agent al-plan`(service atlas-agent-al-plan) trace_id·parentSpanId = 요청 traceparent |
| E4-04 | PASS | `chat planner-default`(service atlas-gateway) parent = invoke_agent span → 에이전트→Gateway 단일 trace |
| E4-05 | PASS | gen_ai.operation.name=chat · provider=mock · request=planner-default · response=mock-planner · usage in/out > 0 · atlas.cost.usd · atlas.unit_id=AL-PLAN |
| E4-06 | PASS | 응답 `x-atlas-trace-id` = 요청 trace_id, `gw.request.completed`(CloudEvents 1.0) tags.trace 동일 |
| E4-07 | PASS | prod 리허설에서 E4-02~06 동일 |
| E4-08 | PASS | `make test-all` exit 0 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 bug | 원인 cause | 수정 fix | 재시험 retest |
|-------|----------|------------|----------|---------------|
| 1 | 컨테이너에서만 `test_invoke_agent_span_joins_caller_trace` 실패 | compose 가 주입한 `OTEL_EXPORTER_OTLP_ENDPOINT` 로 앱이 먼저 전역 TracerProvider 를 설정 → 시험용 provider 무시 | `test/conftest.py` 에서 해당 env 제거 (시험은 in-memory exporter) | round 2: 28 passed |

## 참고

- M0 싱크는 file exporter(검증용). ClickHouse 저장·검색(FR-OB-TRACE-07)·tail sampling·테넌트 헤더 매핑은 M1.
- Manager 는 아직 trace 를 내보내지 않음(M0 범위는 에이전트·Gateway 요청 경로).

## 첨부 / Logs

`logs/` (비커밋) · 재실행: `e4_trace.py <local|prod> [agent] [gateway] [gateway_key]`, `e4-prod.sh`
