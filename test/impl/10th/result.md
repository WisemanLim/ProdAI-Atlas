# 시험 결과 / Test Result — impl 10th · Epic 10 Evals (+ M1 2차 완료 게이트)

- 일자 Date: 2026-10-02
- 대상 Target: `units/ev`(골든셋·검수·불변 버전·trace 승격·스위트·채점기·타깃 3종·pass^k·게이트, compose `agent-ev` :8007) · CLI `python -m units.ev.cli`(exit 0/100/2) · `make eval-seed`·`make eval` · `.github/workflows/eval-gate.yml` · 예제 `evals/planner-golden.jsonl`·`planner-smoke.yaml` · Gateway 태그 `eval_run` · alembic 0006
- 커밋 Commit: — (git 저장소 아님)

## 요약 / Summary

- 전체 Total: 11, 통과 Pass: 11, 실패 Fail: 0, 최종 Verdict: **PASS** (round 3)
- **M1 2차 완료 게이트** `m1b-gate.sh`: test-all + 레드팀 + prod(migrate·db-ping·mcp-seed) + Epic 1~10 스모크 **79 PASS / 0 FAIL**.
- 게이트 재실행에서 **제품 결함 1건 발견·수정**: trace 승격이 데이터셋 입력 스키마를 건너뛰어 형식이 다른 항목이 골든셋에 섞임.

## 케이스 / Cases

| id | 결과 result | 비고 note |
|----|-------------|-----------|
| E10-01 | PASS | agent pytest 110 (EV 11: CRUD·검수·불변 버전·bulk·승격·스키마 PATCH·lint 4종·채점기·pass^k·게이트·기준선·CLI exit) |
| E10-02 | PASS | 3 항목 bulk → in_review → approved(큐레이터) → 스냅샷 |
| E10-03 | PASS (round 3) | prompt·agent 타깃 × epochs 3 → gate PASS, pass_rate 1.0, pass^k 1.0, 비용 집계 |
| E10-04 | PASS | 불가능한 채점기 → exit 100, 사유 `pass_rate 0.0 < 1.0` |
| E10-05 | PASS | 없는 데이터셋 → exit 2 |
| E10-06 | PASS | 전화번호 포함 trace → support-golden in_review, `***-****-3333`, source_trace_id. 계획 골든셋 승격 시도는 422(형식 불일치) |
| E10-07 | PASS | 비큐레이터 403 → 큐레이터 approved, 라벨 이력 actor=curator |
| E10-08 | PASS | 승인 항목 PATCH 409, 새 스냅샷에 승격 항목, 이전 버전 불변 |
| E10-09 | PASS | OB `/v1/costs?group_by=agent` 에 EV-REG 비용 |
| E10-10 | PASS | prod 리허설(게이트에 포함) 동일 |
| E10-11 | PASS (round 3) | M1 2차 게이트 79 PASS |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 bug | 원인 cause | 수정 fix | 재시험 retest |
|-------|----------|------------|----------|---------------|
| 0 (시험 코드) | 검수 시험 403≠409 | 권한 검사가 전이 검증보다 먼저(의도된 동작) | 시험을 큐레이터 헤더로 전이 위반 확인 | 통과 |
| 1 (시험 인프라) | 게이트 round 1: E7-08·E7-09 FAIL | 시험 스크립트가 compose 파일 목록을 하드코딩 → Epic 9 의 `compose/clickhouse.yml` 누락으로 compose 구성 오류, mcp-demo 오염 재기동 실패 | 스크립트 7개가 `compose/*.yml` 을 동적으로 포함(Makefile 과 동일) | round 2 해당 케이스 PASS |
| 2 (**제품**) | 게이트 round 2: E10-03 FAIL (errors 3) | `items:from-trace` 가 `input_schema` 검증을 건너뜀 → 직전 실행의 승격 항목(`{"prompt"}`)이 계획 골든셋(`{"goal"}`)에 섞여 스냅샷·스위트 오염 | 승격도 스키마 검증(422), `PATCH /v1/datasets/{d}`(활성 항목 검사 후 스키마 지정), 시드가 스키마 지정, E10 은 별도 support-golden 으로 승격, 리허설 DB 잔여 항목 retire | round 3: 79 PASS |

## 참고 (범위 밖)

- promptfoo 설정 import·python 샌드박스·state_diff 채점기·LLM-as-Judge(EV-JUDGE, M3)·Parquet/S3 동결·Git 연동·PR 코멘트 diff.
- 배포 게이트 연동(Manager 가 평가 통과 run 을 요구)은 후속 — 현재는 CI 게이트(`eval-gate.yml`)로 병합 차단.
- 큐레이터 권한은 토큰 대체 — PLT-IAM 역할 eval.curator(M2).

## 첨부 / Logs

`logs/` (비커밋) · 재실행: `make eval-seed`, `make eval SUITE=…`, `e10-ev.sh <mode> <key> <curator>`, `m1b-gate.sh`(M1 2차 완료 게이트)
