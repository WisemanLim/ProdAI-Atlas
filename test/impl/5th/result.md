# 시험 결과 / Test Result — impl 5th · Epic 5 배포 E2E + 콘솔 v0 (M0 종료 게이트)

- 일자 Date: 2026-10-02
- 대상 Target: Manager 배포·프로버·agent-proxy(`internal/deploy`, `internal/api/deployments.go`, 마이그레이션 00002·00003) · SDK 배포 등록 · 콘솔 v0(개요 맵·에이전트 표·AL-PLAN 체험)
- 커밋 Commit: — (git 저장소 아님)

## 요약 / Summary

- 전체 Total: 11, 통과 Pass: 11, 실패 Fail: 0, 최종 Verdict: **PASS** (round 2 — 제품 결함 1건 수정)
- **M0 종료 게이트(PRD §20.1) 충족**: 에이전트 1개(AL-PLAN)가 단독 실행(E5-10)되고, 배포 등록 → ready → Manager 경유 A2A → Gateway → trace 까지 E2E(E5-02~05) 통과. local·prod 모두.
- 완료 게이트 `m0-gate.sh`: `make test-all` + prod(migrate·db-ping) + Epic 1~5 스모크 36 PASS / 0 FAIL.

## 케이스 / Cases

| id | 결과 result | 비고 note |
|----|-------------|-----------|
| E5-01 | PASS | manager(배포 3종 + 동시성 회귀) · gateway · agent 29 · console 11 |
| E5-02 | PASS | 기동 후 AL-PLAN 배포 `ready`(dev·compose), Agent Card 수집 |
| E5-03 | PASS (round 2) | Manager agent-proxy 경유 COMPLETED, source `gateway:planner-default`, `x-atlas-deployment` 일치 |
| E5-04 | PASS | Manager 경유에서도 invoke_agent(parent=입력) → chat(parent=invoke_agent) |
| E5-05 | PASS | `plt.deploy.started`(CloudEvents 1.0) · `plt.deploy.succeeded`(to=ready) |
| E5-06 | PASS | stop → ready→degraded→failed, proxy 503 → start 후 ready, proxy 200 |
| E5-07 | PASS | DELETE → stopped·proxy 503 → 재기동 시 새 배포 ready |
| E5-08 | PASS | 콘솔 HTML·`/api/v1/agents`·`/health/manager`·`/gw/health` 200. 화면: headless Chrome 스크린샷(`logs/E5-08-console-local.png`) — 3 레이어 × 번호 카드(07·06 보존, A2A 점선), AL-PLAN 가동, GW-* 서비스, 가동 단위 7/46. "계획 만들기" 클릭은 MANUAL(브라우저 확장 미연결) — 같은 API 경로는 E5-03 으로 검증 |
| E5-09 | PASS | prod 리허설에서 E5-02~05·08 동일 (`logs/E5-09-console-prod.png`) |
| E5-10 | PASS | `agents/runtime` 단독 compose: Card·SendMessage(heuristic)·registration.enabled=false |
| E5-11 | PASS | `make test-all` exit 0 (게이트에 포함) |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 bug | 원인 cause | 수정 fix | 재시험 retest |
|-------|----------|------------|----------|---------------|
| 1 | 완료 게이트(prod)에서 E5-03 `x-atlas-deployment` 불일치 | prod 는 uvicorn `--workers 2` → 워커마다 동시에 배포 등록 → `createDeployment` 의 조회-후-생성 경쟁으로 **활성 배포 2개** (local 단일 워커라 미발견) | 마이그레이션 00003: 기존 중복 정리 + `(agent_id, env) WHERE status <> 'stopped'` 부분 유니크 인덱스, 핸들러는 중복 키 시 재조회·갱신. 동시 8요청 회귀 시험 추가(수정 제거 시 FAIL 확인) | round 2: 게이트 36 PASS, local E5 재실행 PASS |

## 첨부 / Logs

`logs/` (비커밋) · 재실행: `e5_e2e.py <local|prod>`, `e5-failover.sh`, `e5-prod.sh`, `e5-standalone.sh`, `m0-gate.sh`(완료 게이트)
