# 시험 결과 / Test Result — impl 13th (Epic 13 경제성 EC-*)

- 일자 Date: 2026-10-02
- 대상 Target: `apps/gateway` `internal/econ`(plan·budget·cache·batch·simulate) + provider 캐시·effort·Batch 어댑터, AL-RUN 예산 종료 연동, 콘솔 05 경제성
- 커밋 Commit: — (git 저장소 아님)
- 환경 Env: Docker 29.8.0 · Compose v5.5.1 · macOS Darwin 25.6.0 · Go 1.26 · mock 공급자

## 요약 / Summary

- 전체 Total: 13, 통과 Pass: 13, 실패 Fail: 0, 최종 Verdict: **PASS** (round 2)
- 단위: go test gateway 7 패키지 ok(econ 11 · server econ 7 신규, -race), pytest 114 passed, vitest 14 passed
- 스택: local E13 11/11 + E3·E6 회귀 18 + 콘솔 UI 17 · prod 리허설 29 PASS(E13 11 · E3 11 · E6 7)

## 케이스 / Cases

| id | 결과 | 비고 |
|----|------|------|
| E13-01 | PASS | mock-chat-large 폐기 44일 전 경고, 가격에 cached_input·cache_write |
| E13-02 | PASS | chat-econ 짧은 요청 → mock-chat-small, pin·긴 요청 유지 |
| E13-03 | PASS (round 2) | classify → chat-small + max_tokens 500→64, `auto`+reasoning → mock-reasoner |
| E13-04 | PASS | effort high→medium, reasoning_tokens 1024, 비추론 모델 `effort:ignored`, `max_tokens:default=512` |
| E13-05 | PASS | 10회 중 9회 HIT-EXACT 비용 0, BYPASS, SKIP-TEMPERATURE |
| E13-06 | PASS | TTL 5m, 2~5회차 cached_tokens>0, hit_ratio 0.799, 경고 없음 |
| E13-07 | PASS (round 2) | key 예산 downgrade → chat-small, org 예산 429 budget_exceeded, state exceeded |
| E13-08 | PASS (round 2) | 누적 증가 → 429 task_budget_exceeded |
| E13-09 | PASS | ended 2/1, 출력 r0·r1·r2, 재제출 round 1, 미인증 401 |
| E13-10 | PASS | 워터폴 5단 단조 감소 |
| E13-11 | PASS | GW 스트림에 3개 subject 모두 존재 (nats-box) |
| E13-12 | PASS | 콘솔 계산 → 워터폴 5행 |
| E13-13 | PASS | prod 29 PASS, gateway `ReadonlyRootfs=true` · UID 65532 |

## 비용 레버 효과 측정 (M2 종료 게이트 근거, prod 리허설 실측 · mock 단가)

| 레버 | 시나리오 | 레버 없음 | 레버 적용 | 절감 |
|------|----------|-----------|-----------|------|
| 규칙 라우팅 | 짧은 요청 1건 chat-econ | $0.00000800 | $0.00000180 | **77.5%** |
| 과업 모델 정책 | classify 요청 chat-large | $0.00015000 | $0.00000205 | **98.6%** |
| exact 캐시 | 같은 요청 10건 | $0.00009500 | $0.00000950 | **90.0%** |
| prompt cache | 긴 고정 system 5건 | $0.00350500 | $0.00117843 | **66.4%** (hit 0.80) |
| Batch API | 2행 | $0.00001400 | $0.00000700 | **50.0%** |
| 시뮬레이터 | 50k tokens/task · 1,000 tasks/day · large 모델 | 월 $8,100 | 월 $273 | **96.6%** (캐시 0.6·배치 0.3·라우팅 0.4·모델 교체) |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 1 (설계) | 기존 단위 시험 3건 실패 — chat-default 가 chat-small 로 라우팅 | 기본 규칙이 chat-default 에 적용 → 기존 동작 변경 | 규칙 라우팅은 opt-in alias `chat-econ`·`feature:classify` 태그로 한정 | PASS |
| 1 | E13-03 `model: auto` 404 | alias 존재 검사가 경제성 정책보다 먼저 | `auto` 허용 후 해석된 alias 로 권한 검사 | round 2 PASS |
| 1 | E13-07 기간 중 만든 예산이 이미 쓴 금액을 못 봄 | 지출 카운터가 예산별로만 누적 | 범위(org·project·key)별 시간 버킷을 항상 누적, 예산 저장 시 현재 기간 합으로 시드 (+ 단위 시험) | round 2 PASS |
| 1 | E13-08 기대값 오류 | 2번째 호출 전 사용량이 80% 미만 | 누적 증가로 판정 (경고 이벤트는 E13-11 에서 확인) | round 2 PASS |

## 가정 / 후속

- 공급사 prompt cache: mock 이 Anthropic 형(쓰기 1.25x·읽기 0.1x)으로 흉내. OpenAI 어댑터는 `prompt_cache_key`·`cached_tokens` 파싱, Anthropic 어댑터(`cache_control` 브레이크포인트)는 공급자 추가 시.
- 배치 결과는 Redis(7일) — S3 호환 저장소·지연 허용 자동 이관(BATCH-03)은 후속. OpenAI Batch 어댑터는 구현했으나 실 호출 미검증(mock 만).
- 예산 잔액은 Redis 실시간 집계 — ClickHouse 대사는 후속. 시뮬레이터는 추정치라 float(청구 계산은 big.Rat).
- 학습형 라우터·semantic 캐시·의미 분류(ROUTE-02~05, CACHE-05·06)는 P1 후속.

## 첨부 / Logs

`logs/` (비커밋): E13-econ-local(.r2) · E13-regress-local · E13-ui-local · ui-local/ · E13-prod · prod-build · local-all
