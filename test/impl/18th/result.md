# 시험 결과 / Test Result — impl 18th (Epic 18 승인함·알림 + 콘솔 M2 · M2 완료 게이트)

- 일자 Date: 2026-10-02
- 대상 Target: Manager `api/approvals.go`(승인함·결정·만료·철회·A2A 재개)·`api/notify.go`(규칙·전달 기록·시험 발송·템플릿)·`internal/notify`(NATS 소비 → 조건 → webhook HMAC·email SMTP·스로틀·SSRF)·마이그레이션 00006·00007(RLS), Gateway 예산 50/80/100% 임계 이벤트, AL-RUN 승인함 자동 등록·`edited_arguments` 재개·철회, compose `mailpit`, 콘솔 승인함·조직 관리·A2A 화면
- 환경 Env: Docker 29.8.0 · macOS Darwin 25.6.0 · Go 1.26 · Python 3.12 · Node 22 · Chrome(호스트, playwright-core)

## 요약 / Summary

- 전체 Total: 12, 통과 Pass: 12, 실패 Fail: 0, 최종 Verdict: **PASS** (local round 5 · prod round 2)
- local 10/10 (E18-10 은 prod 전용) · prod(oidc) 11/11 · 콘솔 M2 E2E 5/5 (local·prod)
- 단위: go test manager(approvals 2·notify 3 신규) ok · pytest 134 passed(+1) · vitest 19 passed(+3)
- **M2 완료 게이트 `m2-gate.sh`: 205 PASS · 0 FAIL (round 2)** — test-all · 레드팀 · prod migrate·db-ping · Epic 1~18 스모크 · SSO UI(v1·M2) · CLI SSO · RLS · OpenBao · 이미지 하드닝 17종

## 케이스 / Cases

| id | 결과 | 비고 |
|----|------|------|
| E18-01 | PASS | INPUT_REQUIRED → 승인함 loop·AL-RUN·high·payload 해시 |
| E18-02 | PASS | critical → high(300s) → high(7200s) → low |
| E18-03 | PASS | edited → 같은 Task COMPLETED · hitl_edit · 재결정 409 |
| E18-04 | PASS | HITL_DENIED · 사유 기록 |
| E18-05 | PASS | 감사 detail payload 해시 · 체인 valid |
| E18-06 | PASS | unsafe_url 400 · secret `***` |
| E18-07 | PASS | webhook 1(HMAC·80%) · 100% 스로틀 · Mailpit 1(CRITICAL) |
| E18-08 | PASS | 쿼터 429 → webhook · 시험 발송 |
| E18-09 | PASS | 60초 만료 → expired → deny → HITL_DENIED |
| E18-10 | PASS (prod) | read 키 결정 403 · 남의 조직 403 |
| E18-11 | PASS | 직접 재개 → withdrawn |
| E18-12 | PASS | 콘솔 M2 E2E 5/5 (local :5173 · prod :8080 Keycloak 로그인) |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 단위 | 자기 승인 403 (다른 키인데) | 요청자 비교를 API 키 이름으로 함 — 이름은 겹칠 수 있음 | 고유 주체(`Sub`)로 요청자·결정자 기록·비교 | PASS |
| 단위 | 만료 시험 미동작 | SQLite 문자열 시간 비교에 로컬 TZ 값 사용(시험 코드) | UTC | PASS |
| local 1 | E18-05 해시 없음 | 감사 before/after 는 해시로만 저장 | payload 해시를 detail 에도 기록 | local 2 PASS |
| local 3 | E18-11 철회 안 됨 | local manager 는 소스 바인드 `go run` — 재시작 전 옛 코드 | 재시작 (README 트러블슈팅) | local 4 PASS |
| E2E | 레지스트리 첫 행이 검색 전 목록 | 점수 열 헤더가 데이터보다 먼저 바뀜(시험 대기 조건) | 첫 행이 결과로 바뀔 때까지 대기 | PASS |
| E2E | (회귀 예방) 기존 E11 `승인` 버튼 선택 | nav `승인함` 과 부분 일치 | `exact: true` | E11 17/17 PASS |
| prod 1 | E18-06·07 403 | 시험 주체는 default 조직 관리자 — 임의 조직 규칙 생성 불가(정상 인가) | 규칙은 default 조직, 예산은 가상키 범위 + `budget_id` 조건으로 격리, 끝에 규칙 삭제 | prod 2 PASS |
| gate 1 | test-all: AL-RUN 시험 실패(컨테이너만) | 시험이 `httpx.AsyncClient` 를 전역 패치 · 컨테이너 `ATLAS_MANAGER_TOKEN` 으로 실제 토큰 발급 시도 | AL-RUN 전용 `_http` 팩토리만 교체 · 시험에서 env 제거 | gate 2 PASS |
| gate 1 | E7-05 도구 13 ≠ 10 | Manager OpenAPI→MCP 읽기 GET 이 M2 에서 3개 늘어남(getAgentCard·searchA2ARegistry·listNotificationTemplates) — 의도된 변화 | `test/impl/7th/e7_mcp.py` 기대값 13 (주석으로 근거) · 승인함 API 는 MCP 노출 제외 | gate 2 PASS |

## 설계 메모

- 승인함은 Manager 가 정본: 요청(에이전트 자기 토큰 또는 invoke 권한) → 위험도·만료순 목록 → operator 이상·요청자 본인 불가 결정 → 낙관적 잠금(pending 일 때만) → 감사 → 같은 A2A Task 에 `SendMessage {approval, edited_arguments}`(Manager 가 테넌트 JWT 부착). 만료기 15초 주기, 만료 = deny.
- AL-RUN 은 HITL 대기 시 승인함에 자동 등록(같은 대기는 1회), 같은 Task 로 직접 응답이 오면 대기 요청을 철회한다.
- 알림: NATS JetStream 지속 소비자(`atlas-notifier-plt`·`-gw`) → 조직 규칙(topic 패턴 + 선언형 조건 `== != > >= < <= in contains`) → webhook(HMAC `X-Atlas-Signature: t=,v1=`, generic·slack, 5xx/429 재시도 3회) · email(SMTP). 스로틀은 (규칙, subject) 단위. 서명 키는 응답에서 가림.
- 승인함 API(`/api/v1/approvals*`)는 OpenAPI→MCP 도구에서 제외(payload 민감).

## 가정 / 후속

- 도구·배포 경로의 승인 요청은 API 로 직접 등록(루프 HITL 만 자동). 2인 승인·위임 정책, Slack/Teams 대화형 승인, 다이제스트·에스컬레이션은 P1.
- Mailpit 은 개발·리허설용 — 운영은 `ATLAS_SMTP_ADDR` 를 사내 릴레이로.

## 첨부 / Logs

`logs/` (비커밋): local-all(.r3·.r5) · E18-local(.r2~.r5) · E18-prod(.r2) · E18-ui-local(.r2·.r3) · ui-local · ui-prod · E11-ui-regress · test-all · m2-gate(.r2) · gate-*
