# 시험 시나리오 — impl 2nd · Epic 2 Atlas Agent Spec + Python SDK

근거: PRD FR-A2A-SRV-01·02·04·06, FR-A2A-CARD-01, §11.13 패키징 표준, §11.14 매니페스트, FR-AL-PLAN-01·02, NFR-PERF-03, COMPLIANCE CMP-01(Task tenant 파티션 범위).

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E2-01 | 단위 시험 | `make test APP_SVC=agent-al-plan` | pytest 26 passed (Card·JSON-RPC 오류 6종·상태머신·tenant·페이징·INPUT_REQUIRED·취소·백그라운드·Planner·자가등록) | P0 |
| E2-02 | 마이그레이션 | 기동 시 alembic upgrade head (local SQLite) | `a2a_tasks` 생성 | P0 |
| E2-03 | Agent Card | `GET :8000/.well-known/agent-card.json` | v1.0 필수 8필드, url=`http://agent-al-plan:8000/a2a` | P0 |
| E2-04 | SendMessage | JSON-RPC `SendMessage` (목표 텍스트) | `TASK_STATE_COMPLETED`, artifact Plan{steps,dod} | P0 |
| E2-05 | GetTask·ListTasks | 위 task id | 동일 task, 목록 포함 | P0 |
| E2-06 | tenant 격리 | tenant=org-a 로 생성 → org-b 로 GetTask | -32001 | P0 |
| E2-07 | 자가 등록 | Manager `GET /api/v1/agents?unit=AL-PLAN` | `al-plan` 1건, `/readyz` registration.registered=true | P0 |
| E2-08 | 재기동 시 no-op | agent 재시작 후 Manager rev | rev 변화 없음 (같은 spec) | P1 |
| E2-09 | fail-static | Manager 중지 상태로 agent 재시작 | agent ready, A2A 정상, registration.error 기록 → Manager 기동 후 등록 | P0 |
| E2-10 | 콜드 스타트 | 컨테이너 restart → `/readyz` 200 까지 | ≤ 5 s (NFR-PERF-03) | P1 |
| E2-11 | prod(PostgreSQL) | `make prod-all` → E2-03~07 | 동일 결과 | P0 |
| E2-12 | 회귀 | `make test-all` | exit 0 | P0 |
