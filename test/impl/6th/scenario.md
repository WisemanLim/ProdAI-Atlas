# 시험 시나리오 — impl 6th · Epic 6 Agentic Loop 런타임

근거: PRD FR-AL-PLAN-05 · AL-ACT-01·02·03·05 · AL-OBS-01·02·03 · AL-REF-01·02 · AL-EXIT-01·02·03·05·06·07·08 · AL-RUN-01·02·03·05·08, §6.13 Run 상태 전이, §6.14 ExitReason.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E6-01 | 단위 시험 | `make test-all` | gateway(tool calling·mock-agent) · agent pytest(루프 17 + A2A 5 포함) 통과 | P0 |
| E6-02 | 배포 | 스택 기동 | AL-RUN 자가 등록·배포 ready (카드 skills loop.run·loop.resume) | P0 |
| E6-03 | 목표 검증 종료 | Manager proxy → loop.run (calc + DoD contains) | COMPLETED, reason GOAL_VERIFIED, plan 은 원격 AL-PLAN(gateway:planner-default) | P0 |
| E6-04 | HITL | atlas_delete_all → approve | INPUT_REQUIRED → 같은 Task 에 approve → COMPLETED(END_TURN) | P0 |
| E6-05 | 반복 탐지 | repeat-forever | LOOP_DETECTED, 이벤트 replan(원격 plan.revise) | P0 |
| E6-06 | 턴 상한 | never-finish, budget.max_turns=3 | MAX_TURNS, turns=3, status truncated | P0 |
| E6-07 | 전 구간 trace | E6-03 + traceparent | invoke_agent al-run → invoke_agent al-plan, chat agent-default 가 al-run 하위 | P0 |
| E6-08 | 이벤트 스트림 | `GET :8001/runs/{id}/events` | plan → tool_call → observation → reflect → exit | P0 |
| E6-09 | 재시작 후 재개 | HITL 대기 중 al-run 컨테이너 재시작 → approve | 체크포인트에서 이어 COMPLETED | P0 |
| E6-10 | prod 리허설 | `make prod-all` → E6-02~06·09 | 동일 | P0 |
| E6-11 | 회귀 | `make test-all` | exit 0 | P0 |
