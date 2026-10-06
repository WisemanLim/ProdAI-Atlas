# 시험 시나리오 / Test Scenario — impl 28th (Epic 28 엔터프라이즈 IAM · 잔여 P0·P1 1차 게이트)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` PLT-IAM-03·05·07·09 · PLT-TEN-04 · X-WSP-08
- 범위 결정 (2026-10-02, 사용자): 잔여 P1 감사 후 Epic 25~
- 실행: `e28_iam.py` 가 실제 Manager(SCIM `/scim/v2` · `/token/exchange` · 세션 정책 · 예산 · 요금제)·OpenFGA(Conditions)·Keycloak(시험 사용자·직접 로그인 클라이언트, 실행 후 삭제)·Gateway(계층 예산 집행·사용량 이벤트)·NATS 를 호출.
  local(auth off)은 OpenFGA·Keycloak 없이 API 계약·Gateway 연동만, prod(oidc)는 권한 효과까지 검증.
- 게이트: `gate.sh` = M3 게이트(Epic 1~24) + Epic 25~28 prod 스택 시험 + 신규 서비스 하드닝.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E28-01 | SCIM 2.0 (IAM-03) | ServiceProviderConfig · 비 SCIM 키 · 사용자 생성·중복·filter · 그룹+멤버 | 200 · 401 · 201 `application/scim+json` · 409 uniqueness · 1건 · 멤버 1 | P0 |
| E28-02 | 그룹 → 역할 (IAM-03) | 매핑 전 접근 · `admins → org-admin` 매핑 · 사용자 API 키 | 403 → 200 · 키 발급·사용 | P0 |
| E28-03 | 비활성화 = 접근 회수 (IAM-03) | PATCH active=false | 토큰 401 user_deactivated · 소유 API 키 즉시 폐기 | P0 |
| E28-04 | ABAC 조건 (IAM-05) | env-deployer + business_hours(지금/2시간 뒤) · ip_allowlist(0.0.0.0/0 · 10.255.255.0/24) · billing + amount_limit(100) · 조건 불가 역할 | 허용·거부·허용·거부 · 50 허용·500 거부 · 400 | P0 |
| E28-05 | MFA (IAM-09) | 조직 mfaAdmin · acr=1 사용자 관리 API · 조회 API | 401 mfa_required + `insufficient_user_authentication` · 조회 200 | P0 |
| E28-06 | 세션 정책 (IAM-09) | 동시 세션 1 · 유휴 4초 · 최대 2초 | 이전 세션 session_revoked · session_idle · session_expired | P1 |
| E28-07 | 워크로드 신원 (IAM-07) | 에이전트 토큰 aud=agent:{peer} · 없는 peer · mcp:{name} | spiffe://atlas/{org}/{proj}/{env}/{agent} · 400 · 200 | P1 |
| E28-08 | Token Exchange (IAM-07, RFC 8693) | 범위 확대 · 행위자 없음 · read 위임 → 조회·쓰기 | invalid_scope · 401 · act.sub=에이전트 · 200 · 403 | P0 |
| E28-09 | 예산 상속 (TEN-04) | 조직 → 프로젝트 → 환경, 상위 초과·하위 미달 · 환경 키 호출 | 422 exceeds_parent · 422 below_children · Gateway env 예산 집행 429 · 다른 env 통과 | P0 |
| E28-10 | 요금제 할당량 (X-WSP-08) | max_agents 1 · monthly_tokens 40 · 관리 키 요금제 수정 | 2번째 에이전트 429 · 사용량 ≥ 100% · 실행 429 tokens.monthly · 403 | P0 |
| E28-11 | 이벤트 | NATS PLT | plt.iam.user_deprovisioned · plt.quota.threshold · plt.quota.exceeded · (prod) plt.iam.session_revoked | P1 |
| 단위 | go test (SCIM·세션·MFA·위임·예산·요금제 · 모델 조건 · Gateway env 범위) | — | PASS | P0 |
| 게이트 | `gate.sh` — test-all · 레드팀 · Epic 1~28 prod · 콘솔·CLI SSO · 하드닝(+agent-mcp-gw·mcp-protected) | — | rc=0 | P0 |
