# 시험 결과 / Test Result — impl 25th (Epic 25 배포·릴리스 고도화)

- 일자 Date: 2026-10-02
- 대상 Target: Manager 배포 슬롯(primary·canary·standby)·가중 라우팅(고정 배정 키)·롤아웃(canary·bluegreen·평가 게이트·오류율 자동 롤백)·롤백(standby 맞바꿈·리비전)·환경 승격(승인함 `source=deploy`)·배포 정책(digest·허용 레지스트리·ed25519 매니페스트 서명·승인)·드리프트(prober)·스케일 선언·k8s 매니페스트 렌더·관찰 토폴로지, 마이그레이션 00008·00009(RLS), `POST /api/v1/manifests/digest`, 게이트웨이 전용 토큰 대상(`?audience=atlas-mcp-gateway`), 가짜 A2A 에이전트(fake-upstream)
- 환경 Env: Docker 29.8.0 · macOS Darwin 25.6.0 · Go 1.26 · PostgreSQL 17 · `MANAGER_PORT=18080`

## 요약 / Summary

- 전체 Total: 11, 통과 Pass: 11, 실패 Fail: 0, 최종 Verdict: **PASS** (local round 2 · prod round 2)
- 단위: go test 전 패키지 ok (+4: rollouts_test 3 · 게이트웨이 토큰 대상 1) · pytest test_mcp (Manager API MCP 도구 9개 유지)

## 케이스 / Cases

| id | local | prod | 비고 |
|----|-------|------|------|
| E25-01 | PASS | PASS | 30% 가중 · 고정 배정 · 중복 409 |
| E25-02 | PASS | PASS | 60% → 승격 · standby 보존 · 맞바꿈 롤백 |
| E25-03 | PASS | PASS | 오류율 자동 롤백 → primary 만 |
| E25-04 | PASS | PASS | 드리프트(version 9.9.9) |
| E25-05 | PASS | PASS | 블루그린 0% → 전환 |
| E25-06 | PASS | PASS | 평가 게이트 통과·실패 롤백 |
| E25-07 | PASS | PASS | 정책 위반 3건 · 서명 메시지 API |
| E25-08 | PASS | PASS | 승인함 deploy.promote → staging rev 2 |
| E25-09 | PASS | PASS | replicas 3 · Deployment+Service 보안 기본값 |
| E25-10 | PASS | PASS | 토폴로지 노드·간선 |
| E25-11 | PASS | PASS | plt.deploy.* 9종 · deploy.* 감사 8종 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 단위 | 서명 검증 항상 실패 | Manager 가 metadata(layer·concept)를 채워 저장 → 서명자가 본 문서와 다름 | 서명 대상 = {name, unitId, spec − signature} · 언어별 직렬화 차이용 `POST /manifests/digest` | PASS |
| 단위 | OpenAPI 라우트 정합 실패 · MCP 도구 수 증가 우려 | 새 경로 명세 누락 · GET 경로가 OpenAPI→MCP 도구가 됨 | openapi.json 갱신 · 배포 제어면(롤아웃·토폴로지·매니페스트)은 MCP 도구에서 제외 | PASS |
| local 1 | 블루그린 green 이 100% | GORM `default:100` 태그가 0 가중치를 DB 기본값으로 바꿈 | 기본값 태그 제거 + `BeforeCreate`(빈 슬롯 = primary) | PASS |
| local 1 | E25-06·11 기대값 | 마지막 단계(100) 전진 = 승격(설계) | 시험을 3단계 롤아웃으로 · 기대값 정정 | PASS |
| prod 1 | 게이트 실패 롤백이 남지 않음 | 4xx 응답이라 요청 트랜잭션(RLS)이 롤백 → 롤백 기록도 함께 사라짐 | 롤백은 요청 트랜잭션 밖(s.DB)에 기록 | PASS |

## 설계 메모

- 유니크 인덱스 (agent, env, slot) WHERE status ≠ stopped — 환경마다 primary·canary·standby 각 1. agent-proxy·카드·승인 콜백은 standby 를 라우팅하지 않는다.
- 자동 롤백 = prober 주기로 `EvaluateRollouts` (canary failed 또는 minCalls 이상에서 오류율 > maxErrorRate). 평가 게이트는 첫 전진 때 EV `/v1/runs` (`{{canary.endpoint}}` 치환).
- 승격 승인은 기존 승인함을 그대로 쓰고(요청자 ≠ 승인자), 결정 콜백이 `applyPromotion`.

## 가정 / 후속

- Manager 는 프로세스를 띄우지 않는다(Docker 소켓 미사용) — compose 의 롤백·스케일은 라우팅·선언 기록, 실제 재기동·`--scale` 은 운영자. kubernetes 는 매니페스트 렌더(적용은 GitOps/kubectl, 클러스터 시험 없음).
- 매니페스트 서명은 내장 ed25519 신뢰 키(cosign keyless·Rekor 는 후속).

## 첨부 / Logs

`logs/e25-local-r1..r2.txt` · `logs/e25-prod-r1..r2.txt`
