# 시험 결과 / Test Result — impl 6th · Epic 6 Agentic Loop 런타임

- 일자 Date: 2026-10-02
- 대상 Target: `units/al_act·al_obs·al_ref·al_exit`(단독 배포 진입점 `agent_spec`) + `units/al_run`(엔진·저장소·A2A·/runs) · AL-PLAN `plan.revise` · Gateway tool calling(`mock-agent`, alias `agent-default`) · compose `agent-al-run`(:8001) · alembic 0002
- 커밋 Commit: — (git 저장소 아님)

## 요약 / Summary

- 전체 Total: 11, 통과 Pass: 11, 실패 Fail: 0, 최종 Verdict: **PASS** (round 1, 재시도 0)
- 예방 수정 1건: 같은 DB 를 쓰는 워커·컨테이너의 동시 alembic 실행을 PostgreSQL advisory lock 으로 직렬화 (impl 5th 배포 경쟁과 같은 계열 위험) — prod 로그 경쟁 오류 0건 확인.

## 케이스 / Cases

| id | 결과 result | 비고 note |
|----|-------------|-----------|
| E6-01 | PASS | gateway tool calling·mock-agent 각본, agent pytest 51 (루프 17: 종료 사유 12종 중 GOAL_VERIFIED·END_TURN·MAX_TURNS·BUDGET_TOKENS·BUDGET_USD·TIMEOUT·HITL_PENDING·HITL_DENIED·USER_ABORT·LOOP_DETECTED·POLICY_VIOLATION·ERROR 전부, 검증·권한·병렬·재시도·멱등·잘라내기·압축·크래시 재개 / A2A 5) |
| E6-02 | PASS | AL-RUN 자가 등록·배포 ready, skills loop.run·loop.resume |
| E6-03 | PASS | Manager proxy → GOAL_VERIFIED, 출력 `[mock:final] 42`, 계획은 원격 AL-PLAN(gateway:planner-default) |
| E6-04 | PASS | INPUT_REQUIRED → 같은 Task approve → COMPLETED(END_TURN) |
| E6-05 | PASS | LOOP_DETECTED, replan 이벤트(원격 plan.revise) |
| E6-06 | PASS | MAX_TURNS, turns 3, truncated |
| E6-07 | PASS | invoke_agent al-run(parent=입력) → invoke_agent al-plan · chat agent-default ×2 모두 al-run 하위 |
| E6-08 | PASS | SSE: plan → tool_call → observation → reflect → reflect → exit |
| E6-09 | PASS | HITL 대기 중 컨테이너 재시작 → 체크포인트에서 approve → COMPLETED |
| E6-10 | PASS | prod(워커 2·PostgreSQL)에서 E6-02~09 동일, 마이그레이션 경쟁 오류 0 |
| E6-11 | PASS | `make test-all` exit 0 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 bug | 원인 cause | 수정 fix | 재시험 retest |
|-------|----------|------------|----------|---------------|
| 0 (예방) | (잠재) prod 기동 시 동시 마이그레이션 충돌 가능 | uvicorn 워커 2 × agent 컨테이너 2 가 같은 PostgreSQL 에 alembic upgrade 를 동시에 실행 | `app/main.py` 마이그레이션을 `pg_advisory_lock` 으로 직렬화, `migrations/env.py` 가 잠금 연결을 재사용 | E6-10 prod 로그 경쟁 오류 0 |

## 참고 (M1 1차 범위 밖)

- 워커 lease 자동 인수(FR-AL-RUN-03 일부): 재개는 `loop.resume`/같은 Task 메시지로 수동 트리거.
- 압축은 client 전략(규칙 요약). 벤더 server compaction·요약 LLM 은 후속.
- DoD test 러너(pytest 등 샌드박스 실행, FR-AL-REF-02 일부)는 X-SBX(M3). 현재 contains·equals·regex·schema 규칙.
- 스트리밍은 `/runs/{id}/events` SSE. A2A SendStreamingMessage 는 M1 후속.

## 첨부 / Logs

`logs/` (비커밋) · 재실행: `e6_loop.py <local|prod>`, `e6-restart.sh`, `e6-prod.sh`
