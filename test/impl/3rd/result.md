# 시험 결과 / Test Result — impl 3rd · Epic 3 AI Gateway (GW-* 6종)

- 일자 Date: 2026-10-02
- 대상 Target: `apps/gateway` (Go 1.26 · Gin · Redis · NATS), compose 서비스 `gateway`(:8090) + AL-PLAN 연동
- 커밋 Commit: — (git 저장소 아님)

## 요약 / Summary

- 전체 Total: 16, 통과 Pass: 16, 실패 Fail: 0, 최종 Verdict: **PASS**
- 제품 결함 1건(감사 누락)은 단위 시험에서 잡아 수정. 시험 코드 오류 3건 수정.

## 케이스 / Cases

| id | 결과 result | 비고 note |
|----|-------------|-----------|
| E3-01 | PASS | audit·cost·provider·router·server(파이프라인 9종 + OpenAPI 정합) |
| E3-02 | PASS | 200, redis ok |
| E3-03 | PASS | 401 `invalid_api_key` |
| E3-04 | PASS | `sk-atlas-local-<22>` 발급, 200, `x-atlas-cost-usd` 8자리, `x-atlas-route: mock/mock-chat` |
| E3-05 | PASS | 403 `model_not_allowed` |
| E3-06 | PASS | 허용 3회 후 429 `rate_limit_exceeded` + Retry-After |
| E3-07 | PASS | flaky-chat → `x-atlas-fallback: chat-default` |
| E3-08 | PASS | SSE 청크 합 일치, usage 청크, `[DONE]` |
| E3-09 | PASS | requests 2 · tokens · cost_usd > 0 · by_alias |
| E3-10 | PASS | 스트림 `GW` +6 = 완료 요청 6건 |
| E3-11 | PASS | local 12건 · prod `audit chain ok` |
| E3-12 | PASS | AL-PLAN plan.source=`gateway:planner-default`, 3단계, DoD type=test (mock planner) |
| E3-13 | PASS | 폐기 204 → 같은 키 401 (캐시 없음 → 즉시 반영) |
| E3-14 | PASS | 순차 200회: local p95 0.65ms · prod p95 0.50ms (단일 클라이언트·mock 0ms 참고치. NFR-PERF-01 의 4vCPU·500RPS 부하 시험은 M1 k6) |
| E3-15 | PASS | prod 리허설(셸 env 로 리허설 키 주입, runtime 이미지)에서 E3-02~14 동일 통과 |
| E3-16 | PASS | `make test-all` exit 0 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 bug | 원인 cause | 수정 fix | 재시험 retest |
|-------|----------|------------|----------|---------------|
| 0 (구현 중) | 빌드 실패: `field and method with the same name Router` | Server 필드와 메서드 이름 충돌 | 필드명 `Routes` | 빌드 OK |
| 0 (구현 중) | 감사 레코드 4건(기대 5) | `invalid_json`·`missing_field` 거부가 감사 로그를 거치지 않음 (FR-GW-POL-04 누락) | 인증 직후 감사 이벤트 생성, 모든 거부를 `deny` 로 기록 | server 시험 통과 |
| 0 (시험 코드) | usage 조회 실패 | 시험이 prefix 를 23자로 가정(실제 22자) | 저장된 VirtualKey.Prefix 사용 | 통과 |
| 0 (시험 코드) | TPM 429 미발생 | 정산 후 잔여 계산 오류(7+592 ≤ 600) | max_tokens 595 | 통과 |
| 1 (시험 코드) | E3-06 `KeyError: 'error'` | 403 은 정책 단계 거부라 RPM 미소모 — 허용 요청 수 오계산 | 허용 요청 3회 후 429 기대 | 재실행 PASS |

## 첨부 / Logs

`logs/` (비커밋) · 재실행: `e3_smoke.py <admin> <env>`, `e3-audit.sh local|prod`, `e3_latency.py <admin>`, `e3-prod.sh`
