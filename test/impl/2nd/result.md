# 시험 결과 / Test Result — impl 2nd · Epic 2 Atlas Agent Spec + Python SDK

- 일자 Date: 2026-10-02
- 대상 Target: `agents/runtime` — `atlas_agent_core`(SDK) + `units/al_plan`(AL-PLAN Planner), compose 서비스 `agent-al-plan`
- 커밋 Commit: — (git 저장소 아님)

## 요약 / Summary

- 전체 Total: 12, 통과 Pass: 12, 실패 Fail: 0, 최종 Verdict: **PASS** (제품 수정 1건은 구현 중 단위 시험에서 잡음. 시험 스크립트 결함 2건은 재작성 후 재실행)

## 케이스 / Cases

| id | 결과 result | 비고 note |
|----|-------------|-----------|
| E2-01 | PASS | pytest 26 passed (컨테이너 Python 3.12) |
| E2-02 | PASS | 기동 시 alembic upgrade head, `a2a_tasks` 생성(SQLite·PostgreSQL) |
| E2-03 | PASS | Card v1.0 필수 8필드, url `http://agent-al-plan:8000/a2a` |
| E2-04 | PASS | `TASK_STATE_COMPLETED`, Plan 3단계 + DoD |
| E2-05 | PASS | GetTask 동일 id, ListTasks 에 포함 |
| E2-06 | PASS | 다른 tenant 의 GetTask → -32001 |
| E2-07 | PASS | Manager 에 `al-plan` 등록, `/readyz` registration.registered=true |
| E2-08 | PASS | 재시작 후 rev 1 유지 (같은 spec → PATCH no-op) |
| E2-09 | PASS | Manager 중지 중: agent ready + A2A 정상, `registration.error=ConnectError` → Manager 기동 후 자동 등록 |
| E2-10 | PASS | 컨테이너 start → `/readyz` 200: 2.01s · 1.98s · 1.97s (≤ 5s, local dev 스테이지) |
| E2-11 | PASS | prod(PostgreSQL 17, runtime 이미지)에서 E2-03~07 동일 통과 |
| E2-12 | PASS | `make test-all` exit 0 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 bug | 원인 cause | 수정 fix | 재시험 retest |
|-------|----------|------------|----------|---------------|
| 0 (구현 중) | 단위 시험 7건 `DetachedInstanceError` | TaskStore 가 커밋 후 만료된 ORM 객체를 반환 | `Session(..., expire_on_commit=False)` | 26 passed |
| 1 (시험 스크립트) | bash 스모크가 불일치 값을 PASS 로 출력 | 표현식 안 `{'a','b'}` 가 bash brace expansion 으로 인자 분할 | 스모크를 `e2_smoke.py`(Python) 로 재작성 | 재실행 PASS (값 검증) |
| 1 (시험 스크립트) | 수명주기 시험의 restart 가 0.01s (명령 미실행) | 호스트 셸 zsh 는 `$DC` 를 단어 분리하지 않음 | `e2-lifecycle.sh`(bash 배열) 로 재작성 | 재실행 PASS |

## 참고

- pytest 경고 1건: Starlette TestClient httpx deprecation (dev-env 와 동일, 기능 영향 없음).
- A2A 는 JSON-RPC 바인딩 최소 구현(가정 — `docs/implementation-plan.md`). 스트리밍·Push·gRPC·인증 스킴은 M1/M2.

## 첨부 / Logs

`logs/` (비커밋) · 재실행: `e2_smoke.py [agent_url] [manager_url]`, `e2-lifecycle.sh`
