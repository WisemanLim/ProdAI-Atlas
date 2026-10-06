# 시험 시나리오 / Test Scenario — impl 14th (Epic 14 기업·IAM·격리)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` §11 PLT-TEN-01·02·03·05·06 · PLT-IAM-01·04·06·08, COMPLIANCE CMP-01(크로스테넌트 누출 차단·RLS), §20.1 M2 종료 게이트 "멀티테넌트 격리 시험"
- 범위 결정 (2026-10-02, 사용자): M2 1차 Epic 13~15 · IAM = Keycloak + OpenFGA 컨테이너
- 대상 스택: `ATLAS_AUTH_MODE=oidc` (local 은 셸 env 로 켬, prod 는 `.env.prod` 기본). 시험 계정·클라이언트는 Keycloak 관리 API 로 실행마다 무작위 비밀번호로 만들고 지운다.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E14-01 | OIDC 검증 경로 (IAM-01) | `/api/v1/auth/config` · 무토큰 · 위조 토큰 · 공개 경로 · 사용자 토큰 | mode=oidc, 401/401, catalog·openapi·jwks 200, `atlas_org` 클레임 → 조직 문맥 | P0 |
| E14-02 | 조직 수명 (TEN-01·02) | 비관리자·플랫폼 관리자의 `POST /orgs` | 403 / 201 active·shared, default 프로젝트 + dev·staging·prod | P0 |
| E14-03 | RBAC 역할 템플릿 (IAM-04) | 소유자가 프로젝트·역할 부여 | 개발자 생성 O·배포 X → env-deployer 부여 후 staging 배포 O, viewer 읽기 O·쓰기 X | P0 |
| E14-04 | 크로스테넌트 차단 (CMP-01) | beta 사용자가 acme 자원 조회 | 404·403, 목록 0, 조직·감사 403, 프로젝트 역할만 있는 사용자의 조직 목록 비어 있음 | P0 |
| E14-05 | 쿼터 (TEN-03) | 조직 agents.max=2 · 프로젝트 3 | 3번째 429 quota_exceeded, 하위 합 > 상위 422 | P0 |
| E14-06 | 서비스 계정 (IAM-06) | Keycloak client credentials(atlas-ci) | kind=service, 역할 부여 전 403 → deployer 후 201 | P0 |
| E14-07 | API 키 (IAM-06) | `atl_{env}_{ulid}_{secret}` 발급·사용·폐기 | 범위·만료 필수(400), read 키 쓰기 403, 폐기 즉시 401 | P0 |
| E14-08 | A2A 테넌트 전파 (TEN-06) | Manager 경유 호출 · tenant 불일치 · 에이전트 직접 위조 토큰 | ES256 JWT + tenant 강제로 COMPLETED, 403 tenant_mismatch(+감사), 에이전트 -32051 | P0 |
| E14-09 | 조직 정지 (TEN-01) | suspended ↔ active | 정지 중 403 org_suspended, 재개 후 정상 | P0 |
| E14-10 | 감사 로그 (IAM-08) | 조직 체인 | 주요 행위 9종 기록, verify valid, 머클 루트 64자, actor 기록 | P0 |
| E14-11 | 조직 내보내기 (TEN-01) | `GET /orgs/{o}/export` | tar.gz: organization·audit.jsonl·agents/*, API 키 해시·salt 없음 | P1 |
| E14-12 | 30일 유예 삭제 (TEN-01) | `DELETE /orgs/{o}` · default | 202 deleting + deleteAfter ≈30일, default 409 | P1 |
| E14-13 | 콘솔 프록시 `/svc` | tenant 일치·불일치·서비스 admin 경로·무토큰 | 200 · 403 · 403 · 401 | P0 |
| E14-14 | 콘솔 SSO (IAM-01 웹) | 브라우저 PKCE 로그인 → 콘솔 전 섹션 UI E2E | 로그인 사용자 표시, E11·E13 UI 시나리오 전부 통과 | P0 |
| E14-15 | CLI SSO (IAM-01 CLI) | `atlas login`(장치 코드) → 브라우저 승인 → `atlas whoami` | 자격증명 파일 0600, principal user·org | P0 |
| E14-16 | PostgreSQL RLS·append-only (TEN-05 · CMP-01 · IAM-08) | `SET ROLE atlas_rls` + `atlas.tenant` 로 직접 SQL | 8개 테이블 RLS+FORCE, 모르는 tenant 0행, 남의 행 0, 교차 INSERT 거부, 문맥 없음 0행, 감사 UPDATE·DELETE 거부 | P0 |
| E14-17 | 회귀 | go/pytest/vitest · prod 에서 E1·E3·E5·E6·E11·E13 스모크(시험 shim 이 게이트 API 키 부착) | 전부 PASS, manager read_only | P0 |
