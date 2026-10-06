# 시험 결과 / Test Result — impl 28th (Epic 28 엔터프라이즈 IAM · 잔여 P0·P1 1차 게이트)

- 일자 Date: 2026-10-02
- 대상 Target: Manager SCIM 2.0 서버(`/scim/v2/Users·Groups`·ServiceProviderConfig·filter·PATCH, 전용 `scim` 범위 키) · 그룹 → 역할 매핑(`scim_group_roles` → OpenFGA `group#member`) · 디프로비저닝(그룹 튜플 제거·`session_revocations`·소유 API 키 폐기·`plt.iam.user_deprovisioned`) · OpenFGA 조건(`business_hours`·`ip_allowlist`·`mfa_verified`·`amount_limit`, 요청 문맥 now·ip·mfa·amount) · `GET /auth/check` · 세션 정책(`org_security`·`auth_sessions` — acr step-up·유휴·최대 수명·동시 세션) · 에이전트 토큰 `spiffe_id`·peer audience · RFC 8693 `POST /token/exchange`(위임 주체 `delegated`) · 예산 상속(`budgets` → Gateway `mgr.{org}.{id}`, Gateway `env` 범위·`org/proj` 범위) · 요금제(`plans`)·월 사용량(`usage_monthly`, GW 스트림 소비)·실행 게이트·`plt.quota.threshold`(`quota_marks`) · 마이그레이션 00012·00013(RLS)
- 환경 Env: Docker 29.8.0 · macOS Darwin 25.6.0 · PostgreSQL 17 · Keycloak · OpenFGA v1.10.2 · Redis · NATS JetStream

## 요약 / Summary

- 전체 Total: 11, 통과 Pass: 11, 실패 Fail: 0, 최종 Verdict: **PASS** (local round 1 · prod round 3)
- 단위: go test ok — Manager +5(`TestSCIMProvisioningAndDeprovision`·`TestSessionPolicyAndMFA`·`TestWorkloadIdentityAndTokenExchange`·`TestBudgetInheritancePushesToGateway`·`TestPlanQuotasAndThresholds`) · iam +1(`TestModelABACConditions`) · Gateway +1(`TestBudgetQualifiedProjectAndEnvScopes`) · OpenAPI 정합(신규 26 경로)
- local(auth off)은 OpenFGA·Keycloak 효과가 없어 E28-02·04·05·06 의 권한 효과는 API 계약(정책 저장·문맥 필드)만 확인 — 효과 판정은 prod.

## 케이스 / Cases

| id | local | prod | 비고 |
|----|-------|------|------|
| E28-01 | PASS | PASS | SCIM 201 `application/scim+json` · 409 uniqueness · filter · (prod) admin 키 401 |
| E28-02 | PASS | PASS | (prod) 매핑 전 403 → 그룹 `admins → org-admin` 후 200 · 사용자 API 키 발급 |
| E28-03 | PASS | PASS | active=false → (prod) 토큰 401 user_deactivated · 키 401 · revokedAt |
| E28-04 | PASS | PASS | (prod) business_hours 지금 허용·2시간 뒤 거부 · cidr 0/0 허용·10.255.255.0/24 거부 · amount 50 허용·500 거부 · viewer 조건 400 |
| E28-05 | PASS | PASS | (prod) acr "1" → 관리 API 401 mfa_required + `insufficient_user_authentication` · 조회 200 |
| E28-06 | PASS | PASS | (prod) 토큰 sid · 동시 세션 1 → 이전 session_revoked · 유휴 4초 session_idle · 최대 2초 session_expired |
| E28-07 | PASS | PASS | `spiffe://atlas/default/default/prod/al-run` · aud `agent:{peer}` · 없는 peer 400 · `mcp:` 200 |
| E28-08 | PASS | PASS | invalid_scope · 비에이전트 행위자 401 · act.sub=에이전트 · 조회 200 · (prod) 쓰기 403 |
| E28-09 | PASS | PASS | 422 exceeds_parent ×2 · 422 below_children · Gateway env 예산 → 2번째 호출 429 budget_exceeded · dev 환경 200 |
| E28-10 | PASS | PASS | 요금제 2번째 에이전트 429 · 월 토큰 ≥ 100% → 실행 429 tokens.monthly · (prod) 조직 관리 키 요금제 403 |
| E28-11 | PASS | PASS | user_deprovisioned · quota.threshold · quota.exceeded · (prod) session_revoked |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 단위 | 시험 조직 생성 중복 | 시드 `default` 조직 존재 | 시드 조직의 plan 갱신 | PASS |
| 설계 | 소액 예산이 0.00 으로 저장될 뻔 | `FloatString(2)` | 소수 8자리 정규화(`decStr`) — Gateway 1e-8 USD 정산과 일치 | PASS |
| prod 1 | E28-01 admin 키로 SCIM 200 | `HasScope` 는 admin 이 모든 범위를 겸함 | SCIM 은 전용 `scim` 범위만 | PASS |
| prod 1 | E28-03 `session_revoked`·키 미폐기 | 세션 검사가 토큰 조직(default)으로 디렉터리 조회 · SCIM userName ≠ IdP 로그인 이름 | 경로 조직 우선 · 시험 userName = IdP 사용자명(IdP 프로비저닝과 동일) | PASS |
| prod 1 | E28-05 플랫폼 관리자 401 | MFA 정책이 사람 계정 전부에 적용(의도) — 시험 순서 문제 | 역할 부여 후 정책 활성 · 이후 정책 변경은 조직 admin API 키 · 세션 정책 시험 후 해제 | PASS |
| prod 2 | E28-08 쓰기 422 | 빈 본문이 인가 전에 매니페스트 검증 | 유효 매니페스트로 시험 | PASS |

## 설계 메모

- SCIM 권한 주체 = `externalId`(IdP sub) 없으면 `userName`. 비활성 사용자는 그 조직 경로 요청에서 즉시 401, 폐기 시각 이전 발급 토큰은 모든 조직에서 401. 재활성화는 멤버십 튜플만 복원(이전 토큰은 계속 무효).
- ABAC: 조건부 직접 관계는 `env-deployer`(business_hours·ip_allowlist·mfa_verified)·`billing`(amount_limit·business_hours) 만. 금액 없는 요청은 amount 0 — 조건 매개변수 누락으로 판정이 오류가 되지 않게.
- MFA·세션은 사람(OIDC user) 주체만 — API 키·서비스 계정·에이전트·위임 토큰은 대상 아님(범위·만료로 통제). 세션 단위는 Keycloak `sid`.
- 위임 토큰은 Manager ES256 서명(aud 기본 `atlas-manager`), 원래 주체 범위 ∩ 요청 범위, 5분. peer 는 JWKS 로 검증.
- 예산: Manager 가 계층 검증·감사, Gateway 가 집행·사용액. 환경 범위는 가상키 `metadata.env`.
- 요금제: 명시 조직 쿼터가 요금제 한도를 덮어쓴다. 동시 실행 카운터는 단일 Manager 인스턴스 메모리(수평 확장 시 Redis 이관 필요).

## 가정 / 후속

- SAML 연합·SPIRE/mTLS(K8s)·외부 IdP 토큰의 직접 교환은 후속. storage_gb 는 표시만(집행 없음).
- Keycloak OTP/WebAuthn 실제 step-up 흐름(acr 2 토큰 획득)은 브라우저 상호작용이라 리허설 제외 — acr 2 통과는 단위 시험.

## 잔여 P0·P1 1차 게이트 — **PASS** (round 8 + 분할 재실행)

`gate.sh` = M3 게이트(Epic 1~24) + E25~E28 prod 스택 시험 + 하드닝 29종(+agent-mcp-gw·mcp-protected). test-all · 레드팀 · 하드닝은 매회 PASS.

| round | PASS | 실패·중단 | 원인 | 조치 |
|-------|------|-----------|------|------|
| 1 | 334 | E7-05 · E9-06/07 · (E18 중단) | 새 GET 경로(`/scim/v2/*`·`/plans`)가 OpenAPI→MCP 도구로 노출 · GR-PII-08 이 입력에서 비밀을 먼저 가림(PRD 대로) · E27 미리보기 기본 강제가 E18 의 승인 요청을 422 | MCP 제외 경로 추가 · E9 기대값 갱신 · (E18 은 round 6 에 발견) |
| 2 | 317 | E9-06/07 · E10-03 · E20-01~03 · E28 중단 | span JSON 이 `<>` 를 이스케이프 · 골든셋 누적(v43, 738 호출) → 부트스트랩 키 600 RPM 초과 | 비교 문자열 수정 · 리허설 RPM 6000 |
| 3 | 319 | E2-07 · E10-03 · E24-UI · E28 중단(OpenFGA 타임아웃) | E25 정리 누락 에이전트 · TPM 초과 · **X-RPT WORM 봉인 경합(겹치는 세그먼트)** | E25 정리 재시도 · TPM 상향 · 봉인 파일 잠금 + 검증 seq 중복 제거(+단위 시험) |
| 4 | 332 | E24-11 · E24-UI | 새 SIEM 싱크가 감사 이력 전체(≈2만) 재생, 수동 flush 상한 1만 · 설치 20초 초과 | 수동 flush 는 끝까지 · 설치 대기 60초 |
| 5 | 336 | E15-04 · (E18 중단) | 비밀 회전 반영 40초 초과(부하) | — |
| 6 | 281 | E10-03 · E20-09 · E21-UI · E24-UI · 다수 중단(비밀 API 502) | OpenBao 재봉인(부하 추정) · **E18 중단 원인 확인: 정책 없는 조직에도 미리보기 강제** | 미리보기 강제는 정책 저장 시 opt-in, E27 원복은 `requirePreviewFor: []`, 리허설 DB 정책 원복 |
| 7 | 331 | E21·E22 중단(OB `/v1/traces/{id}/io` 비JSON) | OB·ClickHouse 응답 불가(부하) | — |

| 8 | 345 | E9-06 · E20-11 (중단 0) | 사용자 결정 반영: Docker 메모리 16 GiB · 리허설 prod 볼륨 27개 초기화 · 분할 실행 허용. E9-06 = 새 스트림에 옛 `gw.secret_leak_detected` 가 없음(가드가 먼저 가려 Gateway 는 원문을 못 봄 — 이전 PASS 는 옛 메시지) · E20-11 = 부하 중 best-of-n 표본 미검증(재현 30/30 verified) | E9 기대값: 가드가 먼저 가렸으면 건수 미검사 |
| 8 분할 | +21 | 없음 | 같은 prod 스택에서 실패 스위트만 재실행: E9 9/9 · E20 12/12 | — |

**최종: round 8 전체 345 PASS + 분할 재실행 21 PASS → 게이트 PASS** (test-all · 레드팀 · Epic 1~28 prod 스모크·콘솔 SSO UI v1·M2·M3·M3b · CLI SSO · RLS · OpenBao · 하드닝 29종).

E25~E28 44건은 완주한 회차마다 전부 PASS. 남은 실패는 회차마다 다른 기존 스위트에서 부하 증상으로 나타나며, 유휴 prod 스택만 Docker VM 7.7 GiB 중 ≈6 GiB(다른 프로젝트 컨테이너 포함)를 쓴다. 리허설 볼륨 초기화(`make db-reset ENV=prod` 류)는 규칙상 하지 않음.
round 1~7 의 남은 실패 원인 = 환경 용량(7.7 GiB VM·누적 볼륨) — 2026-10-04 사용자 결정 (a) 16 GiB (b) 볼륨 초기화 승인 (c) 분할 허용 으로 해소.

## 첨부 / Logs

`logs/e28-local-r1.txt` · `logs/e28-prod-r1..r3.txt` · `logs/gate-r1..r8.txt` · `logs/gate-r8-split-e9.txt` · `logs/gate-r8-split-e20.txt`
