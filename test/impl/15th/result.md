# 시험 결과 / Test Result — impl 15th (Epic 15 비밀 · M2 1차 완료 게이트)

- 일자 Date: 2026-10-02
- 대상 Target: Manager `internal/secrets`(file · OpenBao KV v2) + 비밀 중개 API·에이전트 단기 토큰, Gateway `internal/keyrot`(공급자 키 이중 슬롯 회전), 에이전트 SDK `atlas_agent_core.secrets`(선언 비밀 주입), OpenBao 2.4.4 + `openbao-init`
- 커밋 Commit: — (git 저장소 아님)
- 환경 Env: Docker 29.8.0 · Compose v5.5.1 · macOS Darwin 25.6.0 · Go 1.26 · Python 3.12

## 요약 / Summary

- 전체 Total: 8, 통과 Pass: 8, 실패 Fail: 0, 최종 Verdict: **PASS** (prod r2 + 게이트 r1~r3 에서 E15-07 마지막 검사 재확인)
- 단위: go test manager(secrets file·KV v2 fake · broker API 2 신규) · gateway(keyrot 2 · secrets) ok, pytest 120 passed(+3 SDK 비밀)
- local(file): E15-01~05 5/5 · prod 리허설(OpenBao): E15-01~06 6/6 · OpenBao 성질 11/11
- **M2 1차 완료 게이트** `m2a-gate.sh`: **160 PASS · 0 FAIL** (round 3) — test-all(go 15 패키지 ok · pytest 119 passed/1 skipped · vitest 16), 레드팀, prod(oidc·RLS·OpenBao) 에서 E1~E15 스모크·SSO 콘솔 UI·CLI SSO·RLS 8·OpenBao 11·하드닝 11

## 케이스 / Cases

| id | 결과 | 비고 |
|----|------|------|
| E15-01 | PASS | local backend=file · prod backend=bao, guard-secret(env 이관) 목록, 값 없음 |
| E15-02 | PASS | gr-io 가 중개로 v1 수신, `ATLAS_ALLOW_ENV_SECRETS=false`, 파일 0400(prod tmpfs `/run/secrets/atlas`), 가명화 transform |
| E15-03 | PASS | al-plan 403 secret_not_declared · 관리자 403 agent_only · gr-io 200 `Cache-Control: no-store` |
| E15-04 | PASS | 회전 → previousVersion = v-1, 에이전트 5초 갱신 주기 안에 반영, 값 해시 일치 |
| E15-05 | PASS | secret.read success(gr-io)·denied(al-plan)·secret.rotate, 감사에 값 없음 |
| E15-06 | PASS (prod) | 무효 키 422 key_validation_failed(교체 안 함) · 유효 키 버전+1 · retiring 1 · backend bao · 평문 미노출 |
| E15-07 | PASS (게이트) | 초기화·해제, 루트 토큰 미보관, Manager↔Gateway 정책 상호 거부, 토큰 마운트 읽기 전용, 재기동 → 봉인 → API 502 → openbao-init 로 복구 200 |
| E15-08 | PASS | 경로 순회 거부, 목록·감사 값 미포함, 회전 유예·폐기 이벤트·기한 알림 1회, 외부 회전 반영 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 1 (보안) | `GET /api/v1/secrets/{name}` 이 OpenAPI→MCP 도구로 노출 (pytest 실패로 발견) | MCP-TOOL 은 Manager 명세의 GET 을 전부 도구화 | `EXCLUDE_PREFIX` 에 `/api/v1/secrets` 추가 | PASS |
| prod 1 | openbao-init 이 로그 없이 exit 2 → prod-build 실패 | `set -e` 에서 `bao status`(미초기화 = 2) 가 스크립트 종료 | 종료코드를 `|| rc=$?` 로 수집 | prod r2 PASS |
| prod 2 | token-file-readonly-mount 0 | 쉘 리디렉션 오류가 컨테이너 stderr 로 나가 grep 이 못 봄(시험 결함) | `2>&1` 을 docker exec 바깥에 | 게이트 r1·r2 PASS |
| 게이트 1 | E10-06~08 `trace 에 입력 본문이 없음` 409 | 시험이 `/io` 200(가드 span 먼저 수집)만 기다리고 바로 승격 — 경쟁 조건 | `captured:true` 까지 대기 (impl 10th 스크립트 갱신 주석) | 게이트 r2 |
| 게이트 1 (제품) | exact 캐시 적중 응답의 span 에 본문 없음 | `serveExact` 가 본문 기록(OB-LOG-01)을 건너뜀 → 캐시 적중 trace 는 골든셋 승격 불가 | 캐시 적중에도 `captureBodies` + 단위 시험 | PASS |
| 게이트 2 | E9-03~05 Gateway span 없음 | 시험의 `wait_trace` 가 span 개수만 기다림 — 에이전트·가드 span 이 먼저 채우고 Gateway span 은 늦게 수집(Keycloak·OpenBao 추가로 부하↑) | 필요한 서비스(atlas-gateway) span 까지 대기 (impl 9th 스크립트 갱신 주석) | 게이트 r3 160 PASS |
| 게이트 1 | E8-07 `KeyError: result` | M2 부터 Manager 경유 A2A tenant 는 조직 문맥으로 강제 — 시험이 임의 tenant(`pol-…`)를 Manager 로 보냄(정상 거부 403) | 임의 tenant 정책 시험은 AL-RUN 직접 호출 (impl 8th 스크립트 갱신 주석) | 게이트 r2 |

## 설계 메모

- 비밀 중개 모델: 관리자 write-only, 값은 매니페스트 `spec.secrets[]` 선언 에이전트만 — 서비스 키로 자기 단기 토큰(ES256 15분, aud=atlas-manager) 발급 → 읽기마다 감사. 에이전트별 OpenBao 정책·워크로드 JWT(PRD SEC-08 구현 요지)는 중개 방식으로 대체 — Manager 만 OpenBao 토큰을 가진다.
- 환경변수 비밀은 기본 차단: SDK 는 `ATLAS_ALLOW_ENV_SECRETS=true` 이거나 Manager 없이 단독 실행할 때만 env 를 읽는다. prod 의 agent-gr 은 env_file 에 `ATLAS_GUARD_SECRET` 이 있어도 무시하고 중개 값을 쓴다.
- 공급자 키: 저장소 `providers/{p}` 가 정본(env 키는 최초 1회 이관). 회전 = 새 키 `/models` 검증 → current 교체 → 이전 키 24h retiring(활성 키 장애 시 예비) → `gw.key.retired`. 90일 주기, 14일 전 `gw.key.rotation_due`.
- OpenBao 리허설 한계: unseal 키를 호스트 볼륨(baokeys, 0600)에 보관 → 운영은 KMS/Transit auto-unseal(SEC-02, P1). 재기동 후에는 `openbao-init` 재실행 필요.

## 가정 / 후속

- 나머지 서비스 비밀(ClickHouse·Gateway 관리 토큰·Keycloak 관리자 등)은 아직 env 주입 — 중개 이관은 후속. SEC-02·05·06·07(HA·BYOK·SOPS·키 분리)은 P1.
- 로컬 Gateway 는 공급자 키 저장소 미설정(mock 만) — 키 회전은 prod 리허설에서 Gateway 자신의 OpenAI 호환 `/v1` 을 업스트림으로 검증.

## 첨부 / Logs

`logs/` (비커밋): local-all · E15-local · E15-prod(.r2) · prod-build · m2a-gate(.r2) · gate-ui/ · gate-*
