# 시험 결과 / Test Result — impl 14th (Epic 14 기업·IAM·격리)

- 일자 Date: 2026-10-02
- 대상 Target: Manager `internal/iam`(OIDC·API 키·OpenFGA·A2A 서명·감사 체인) + 테넌시 API·RLS·쿼터, 에이전트 SDK 테넌트 JWT 검증, 콘솔 PKCE 로그인, `atlas login`/`whoami`, Keycloak 26.3 · OpenFGA 1.10.2 컨테이너
- 커밋 Commit: — (git 저장소 아님)
- 환경 Env: Docker 29.8.0 · Compose v5.5.1 · macOS Darwin 25.6.0 · Go 1.26 · Python 3.12 · Chrome(playwright-core)

## 요약 / Summary

- 전체 Total: 17, 통과 Pass: 17, 실패 Fail: 0, 최종 Verdict: **PASS** (local round 2 · prod round 2)
- 단위: go test manager(iam 6 · api 신규 7 포함) ok, pytest 117 passed(+3 tenancy), vitest 16 passed(+2 PKCE)
- local(oidc): E14-01~13 13/13 · 콘솔 UI(SSO 포함) 17/17 · CLI 1/1
- prod 리허설(round 2): **85 PASS · rc 0** — E14 15 + RLS 8 + 콘솔 UI 17 + E1·E3·E5·E6·E11·E13 회귀

## 케이스 / Cases

| id | 결과 | 비고 (prod) |
|----|------|-------------|
| E14-01 | PASS (local r2) | oidc · 401/401 · 공개 200 · `atlas_org` 클레임 → 조직 문맥 |
| E14-02 | PASS | 비관리자 403 · 생성 201 active/shared · 환경 dev/staging/prod |
| E14-03 | PASS | 개발자 생성 201 · prod 배포 403 · env-deployer 후 staging 201 · viewer 읽기 200·쓰기 403 |
| E14-04 | PASS | beta→acme 404/403, 목록 0, 조직·감사 403 |
| E14-05 | PASS | 3번째 429 quota_exceeded · 하위 합 초과 422 |
| E14-06 | PASS | service · 403 → deployer 후 201 |
| E14-07 | PASS | `atl_prod_…` · read 200 · write 403 · 만료 누락 400 · 폐기 401 |
| E14-08 | PASS | 프록시 COMPLETED · 불일치 403 tenant_mismatch · 위조 토큰 -32051 |
| E14-09 | PASS (local r2) | 정지 중 403 org_suspended · 재개 후 201 |
| E14-10 | PASS | 9종 행위 기록 · verify valid · 머클 루트 64자 |
| E14-11 | PASS | tar.gz organization·audit.jsonl·agents/web/*, salt 없음 |
| E14-12 | PASS | 202 deleting · 30일 · default 409 |
| E14-13 | PASS | /svc 200·403·403·401 |
| E14-14 | PASS | 브라우저 PKCE 로그인 → 콘솔(Manager 내장 :8080, local 은 :5173) + E11·E13 UI 전부 |
| E14-15 | PASS | 장치 코드 승인 → 자격증명 0600 → whoami user/default |
| E14-16 | PASS | RLS+FORCE 8 테이블 · 모르는 tenant 0 · 남의 행 0 · 교차 INSERT 거부 · 문맥 없음 0 · 감사 UPDATE/DELETE 거부 |
| E14-17 | PASS (prod r2) | E1 10 · E3 11 · E5 6 · E6 7 · E11 2 · E13 11, manager read_only |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 1 (기동) | Keycloak 종료 `Could not open file …/keycloakdb.mv.db` | named volume 을 `data/h2` 에 붙여 root 소유 → uid 1000 쓰기 불가 | 볼륨을 `/opt/keycloak/data` 에 마운트(이미지 소유권 복사), import 는 하위 바인드 | 기동 healthy |
| 1 | 서비스 계정 토큰에 username·roles 없음 | realm 에 `clientScopes` 를 직접 정의하면 기본 스코프(profile·roles·basic) 가 생성되지 않음 | 클라이언트별 protocolMapper 로 `atlas_org` 매핑, 기본 스코프 유지 | kind=service |
| 1 | E14-01·09 토큰에 `atlas_org` 없음 | KC 26 선언형 사용자 프로필이 미선언 속성을 버림 | realm 에 사용자 프로필 구성(atlas_org: 관리자만 편집) | local r2 PASS |
| 1 | E14-03 멤버 수 기대 4 | 그 시점 튜플은 owner·viewer·developer 3개(환경 역할은 환경 객체) | 기대값 ≥3 | PASS |
| 1 | `go test` 에 `no such table: quota` | GORM 이 Quota 를 단수 테이블로 추정 | `TableName() = quotas` | PASS |
| 1 | svc 프록시 시험 패닉 (CloseNotify) | ResponseRecorder 는 CloseNotifier 미구현(이전 차수와 같은 제약) | 실제 httptest 서버로 호출 | PASS |
| prod 1 | E5-08 401 | 콘솔 nginx(:5173) 경유 `/api` 도 인증 필요(정상) — 시험 shim 이 :8080 만 덮음 | shim 대상에 콘솔 주소 추가 | prod r2 85 PASS |

## 보안 설계 메모

- RLS 는 요청 경로에만: 요청 트랜잭션이 `SET LOCAL ROLE atlas_rls` + `atlas.tenant` 를 건다. 정책은 `current_user <> 'atlas_rls'` 면 통과 — 배경 작업(배포 프로버)·마이그레이션은 영향 없음. 앱 수준 `org_id` 필터와 이중 방어.
- 감사 체인은 조직별(RLS 와 일치), 요청 트랜잭션과 분리된 연결로 기록 → 실패·거부된 요청도 남는다.
- `/svc` 프록시: 사용자 토큰을 하위 서비스로 넘기지 않음(토큰 패스스루 금지), `tenant` 쿼리·본문을 조직 문맥으로 강제, 서비스 `/admin` 경로는 플랫폼 관리자만.
- OpenAPI→MCP 도구에서 `/api/v1/orgs`·`/auth`·`/.well-known` 제외 — 조직·IAM 관리 API 가 에이전트 도구로 새지 않게.
- 에이전트 Manager 호출은 서비스 API 키(`ATLAS_MANAGER_BOOTSTRAP_KEY`, 범위 read·agents:write·deployments:write·a2a:invoke).

## 가정 / 후속

- local 기본 `ATLAS_AUTH_MODE=off`(개발 편의, 모두 관리자). 시험은 셸 env 로 oidc 를 켰다. prod 기본 oidc.
- 시험 하네스 `authshim/`(sitecustomize·curl 래퍼)은 인증 이전 스모크를 그대로 돌리기 위한 것 — 제품 코드 아님, PYTHONPATH·PATH 로 명시할 때만 동작.
- 런타임(Python) 테이블 RLS 는 후속 — 현재 A2A Task·런은 tenant 파티션 + JWT 검증. SAML·SCIM·ABAC·MFA·워크로드 신원(IAM-02·03·05·07·09)은 P1 후속.
- Keycloak issuer 는 `ATLAS_KEYCLOAK_URL`(기본 http://127.0.0.1:8180) — 원격 설치는 공개 주소·TLS 필요.
- A2A 서명 키 미주입 시 임시 키(단일 인스턴스) — 운영은 `ATLAS_A2A_SIGNING_KEY`(번들·CLI 가 생성), 비밀 드라이버는 Epic 15.

## 첨부 / Logs

`logs/` (비커밋): local-all · E14-iam-local(.r2) · E14-ui-local · ui-local/ · E14-cli-local · E14-prod(.r2) · ui-prod/ · prod-build
