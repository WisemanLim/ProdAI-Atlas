# 시험 결과 / Test Result — impl 16th (Epic 16 A2A 3종)

- 일자 Date: 2026-10-02
- 대상 Target: Manager `api/a2a.go`(서명 카드·레지스트리)·agent-proxy SSE 중계·에이전트 주체 위임, SDK `a2a.py`(스트리밍·재구독·Push CRUD·확장 카드·skill 보안)·`push.py`(서명 webhook·재시도·SSRF)·`a2a_client.py`, 마이그레이션 0007
- 환경 Env: Docker 29.8.0 · macOS Darwin 25.6.0 · Go 1.26 · Python 3.12

## 요약 / Summary

- 전체 Total: 12, 통과 Pass: 12, 실패 Fail: 0, 최종 Verdict: **PASS** (local round 2 · prod round 1)
- 단위: go test manager 신규 3(a2a) ok, pytest 129 passed(+9)
- local 11/11 · prod(oidc) 11/11

## 케이스 / Cases

| id | 결과 | 비고 |
|----|------|------|
| E16-01 | PASS | 6개 에이전트 카드 Go 서명 → Python 검증, 변조 거부 |
| E16-02 | PASS | BM25 top al-plan, skill 필터 al-run |
| E16-03 | PASS | skill=loop.plan → al-plan COMPLETED, 단계 3 |
| E16-04 | PASS | 스트림 COMPLETED(42) |
| E16-05 | PASS | submitted → COMPLETED |
| E16-06 | PASS | INPUT_REQUIRED → 승인 재개 → END_TURN |
| E16-07 | PASS (local r2) | CANCELED · timeout |
| E16-08 | PASS | push 수신 + 에이전트 JWKS 로 JWT 검증 |
| E16-09 | PASS | -32602 × 2 |
| E16-10 | PASS | extended true · 직접 -32051 |
| E16-11 | PASS | DepthExceeded |
| E16-12 | PASS | — |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 1 | E16-07 COMPLETED | mock 루프가 1.5초 마감 전에 끝남(시험 조건) | 마감 0.05초 | local r2 PASS |
| 단위 | JCS 시험이 `<` 존재로 실패 | 시험 작성 시 Python 이스케이프로 `<` 가 `<` 로 바뀜 | 문자열 수정 | PASS |
| 단위 | 에이전트 토큰 관리 API 403 기대 → 200 | 이번 Epic 에서 에이전트 주체에 같은 조직 조회·위임 권한 부여(설계) | 쓰기 API 403 으로 시험 갱신 | PASS |

## 설계 메모

- 카드 서명은 Manager(카드 수집·게시 주체)가 한다: 저장된 카드 + Atlas 메타데이터(agentId·org·project·env·visibility) → JCS(키 정렬·공백 없음·HTML 이스케이프 없음) → JWS(ES256, Manager 키 kid) `signatures[]`. 공개키 = Manager `/.well-known/jwks.json`.
- 가시성: 매니페스트 `spec.agentCard.visibility` = private(같은 프로젝트) · tenant(같은 조직, 기본) · public(인증된 모든 조직). 못 보는 카드는 404(존재 은닉).
- 위임은 Manager agent-proxy 경유 — 호출 에이전트는 자기 단기 토큰(aud=atlas-manager)으로 인증, 대상용 테넌트 JWT(aud=agent:{id})는 Manager 가 붙인다(CLI-03). 에이전트 주체 권한은 같은 조직 viewer·invoke 만.
- Push 발신 JWT 서명 키는 컨테이너 안 워커들이 공유하는 파일(/tmp, 0600)에 둔다 — 공개키 = 에이전트 `/.well-known/jwks.json`, 카드 metadata `atlas.pushJwks`.
- 스트리밍 구독은 프로세스 안 큐 — prod 의 다중 워커에서 다른 워커가 실행한 Task 는 스트림 대신 폴링으로 추적된다(클라이언트가 자동 폴백).

## 가정 / 후속

- 외부 카드 수집·버전 관리(CARD-04·05)·위임 트리 콘솔(CLI-05)·Artifact 검사 저장(CLI-08)·v0.3 호환(SRV-07)은 P1. gRPC·HTTP+JSON 바인딩 미구현(카드 우선순위만).
- 임베딩 검색은 미구현 — BM25 만.

## 첨부 / Logs

`logs/` (비커밋): local-all · E16-local(.r2) · E16-prod · prod-build
