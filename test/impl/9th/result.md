# 시험 결과 / Test Result — impl 9th · Epic 9 Observability

- 일자 Date: 2026-10-02
- 대상 Target: ClickHouse 25.8(`compose/clickhouse.yml`, `otel.otel_traces` TTL 30일) · Collector clickhouse exporter · `units/ob`(검색 DSL·트리·세션·재실행·대시보드·비용·SLO·경보, compose `agent-ob` :8006) · Gateway `internal/capture`(본문 기록 정책·비밀 제거)·결정 span 이벤트 · AL-RUN loop/HITL span 이벤트 · SDK: FastAPI 내장 텔레메트리 정리·background 작업 · alembic 0005
- 커밋 Commit: — (git 저장소 아님)

## 요약 / Summary

- 전체 Total: 12, 통과 Pass: 12, 실패 Fail: 0, 최종 Verdict: **PASS** (round 2 — 시험 기대값 2건 수정)
- 구현 중 발견: FastAPI 0.142 내장 텔레메트리가 기본으로 켜져 헬스 폴링·의존성 해석까지 span 으로 저장(저장량 폭증 위험) → 프로브 경로 제외·operation span 끔.

## 케이스 / Cases

| id | 결과 result | 비고 note |
|----|-------------|-----------|
| E9-01 | PASS | gateway capture(비밀 4종 제거·metadata 모드 본문 없음)·span 이벤트, agent pytest 98 passed·1 skipped(OB 14 포함) |
| E9-02 | PASS | otel_traces TTL 30일, 루프 1회 span ≥ 6, 최근 2분 프로브 span 0 |
| E9-03 | PASS | DSL `gen_ai.request.model = "agent-default" AND trace_id = …` → 1건, cost>0, services 3종 |
| E9-04 | PASS | 트리·워터폴(첫 offset 0)·위임 그래프 al-run→gateway, al-run→al-plan |
| E9-05 | PASS | a2a.context.id 세션에 해당 trace |
| E9-06 | PASS (round 2) | span 본문: 전화번호 마스킹(가드), AKIA → [SECRET], `gw.secret_leak_detected` count 2(입력·출력) |
| E9-07 | PASS | Events: atlas.guardrail.decision · atlas.loop.exit · atlas.ob.secret_leak_detected |
| E9-08 | PASS (round 2) | 재실행: 원본 planner-default(첫 chat span) ↔ chat-default 출력 비교 |
| E9-09 | PASS | 모델별 requests·p95, 에이전트별 비용(AL-RUN·AL-PLAN) |
| E9-10 | PASS | latency SLO burn_rate > 1, slo_burn 규칙 firing 1회(재평가 시 중복 없음), local 웹훅 echo 수신·trace 링크 |
| E9-11 | PASS | prod 리허설 동일(웹훅 미설정 → delivered none) |
| E9-12 | PASS | `make test-all` exit 0 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 bug | 원인 cause | 수정 fix | 재시험 retest |
|-------|----------|------------|----------|---------------|
| 0 (구현 중) | ClickHouse 에 `GET /readyz`·`fastapi.dependencies` span 대량 유입 | FastAPI 0.142 내장 텔레메트리 기본 활성 | `create_app` 에서 프로브 경로 exclude, operation_spans·metrics·logs·auto_configure 끔 | E9-02 프로브 span 0 |
| 0 (시험 코드) | gateway capture 시험 panic | OTel 전역 tracer 가 첫 provider 에 고정 → 두 번째 recorder 무시 | 시험 전체 공유 recorder(sync.Once) | 통과 |
| 0 (시험 코드) | 경보 시험 0건 | `httpx.AsyncClient` 통째 대체가 ClickHouse 조회까지 가로챔 | 웹훅 전송을 `send_webhook` 으로 분리해 그것만 대체 | 통과 |
| 1 (시험 코드) | E9-06 count 2≠1, E9-08 모델 불일치 | mock 출력이 입력을 되풀이(입력·출력 모두 키 포함) / 기본 재실행 대상이 첫 chat span(AL-PLAN) | 기대값을 실제 동작 기준으로 수정 | round 2 PASS |

## 참고 (범위 밖·한계)

- TTFT·캐시 적중률 미측정(업스트림 스트림 중계·EC-CACHE 전). 요약 1년 보관(롤업)·tail sampling·테넌트 헤더(x-atlas-project-key) 매핑은 후속.
- 결정 로그는 OTel Logs 대신 trace 에 붙은 span 이벤트로 구현(같은 trace_id 상관).
- 본문 PII 정화는 가드 입력 변환 + Collector 정규식 2단. 가역 토큰화(KMS)는 후속.

## 첨부 / Logs

`logs/` (비커밋) · 재실행: `e9_ob.py <mode> <key> <ch_password>`, `e9-prod.sh`
