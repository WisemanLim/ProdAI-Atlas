# 시험 시나리오 — impl 5th · Epic 5 배포 E2E + 콘솔 v0 (M0 종료 게이트)

근거: PRD §20.1 M0 게이트 "에이전트 1개 독립 실행·배포 E2E", FR-PLT-MGR-02(Card 수집·검증)·04(삭제 시 배포 중지)·05(헬스 수집)·10(agent-proxy), §14.4 `plt.deploy.*`, SCR-B03 개요 맵, NFR-AVL-02(fail-static) 일부.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E5-01 | 단위 시험 | `make test-all` | manager(배포·프로버·proxy 포함)·gateway·agent(29)·console(11) 통과 | P0 |
| E5-02 | 자동 배포 등록·ready | 스택 기동 | AL-PLAN 배포 `ready`, Agent Card 수집됨 | P0 |
| E5-03 | Manager 경유 A2A | `POST /api/v1/agents/{id}/a2a` SendMessage | COMPLETED, plan.source=gateway:planner-default, `x-atlas-deployment` | P0 |
| E5-04 | 전 구간 trace | E5-03 에 traceparent | invoke_agent(parent=입력) → chat(parent=invoke_agent) | P0 |
| E5-05 | 배포 이벤트 | NATS `PLT` 스트림 | `plt.deploy.started`·`plt.deploy.succeeded` 존재 | P0 |
| E5-06 | 장애·복구 | agent 컨테이너 stop → start | degraded→failed, proxy 503 → 재기동 후 ready 복귀 | P0 |
| E5-07 | 배포 중지 | `DELETE /api/v1/deployments/{id}` | stopped, proxy 503 · agent 재기동 시 새 배포 ready | P1 |
| E5-08 | 콘솔 | `:5173/` · `/api/v1/agents` · `/health/manager` · `/gw/health` · 화면 | HTML·JSON 200, 개요 맵에 AL-PLAN 가동·GW 서비스 표시 | P0 |
| E5-09 | prod 리허설 | `make prod-all` → E5-02~05·08 | 동일 | P0 |
| E5-10 | M0 게이트: 단독 실행 | `agents/runtime` 에서 `make local-all` (Manager·Gateway 없이) | Card·SendMessage 정상(heuristic), 등록 비활성 | P0 |
| E5-11 | 회귀 | `make test-all` | exit 0 | P0 |
