# 시험 시나리오 / Test Scenario — impl 15th (Epic 15 비밀)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` §11 PLT-SEC-01·03·04·08, OWASP ASI03(정적 키 공유 금지)
- 범위 결정 (2026-10-02, 사용자): 비밀 = OpenBao 컨테이너 + 파일 드라이버 — local = `file://` (Manager 데이터 볼륨), prod = OpenBao 2.4 KV v2(file 스토리지, openbao-init 이 초기화·봉인 해제·정책·서비스 토큰)
- 원칙: 비밀 값은 응답 목록·감사·로그·시험 출력에 남기지 않는다(길이·해시만 비교). 관리자는 write-only.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E15-01 | 백엔드 추상화 (SEC-01) | `GET /api/v1/orgs/default/secrets` | backend=file(local)·bao(prod), env 이관된 guard-secret 목록, 값 없음 | P0 |
| E15-02 | 주입 드라이버 (SEC-03) | agent-gr(매니페스트 spec.secrets=guard-secret) | 중개로 수신(readyz 버전), `ATLAS_ALLOW_ENV_SECRETS=false`, 파일 0400(tmpfs), 가드 가명화 동작 | P0 |
| E15-03 | 최소 권한 (SEC-08) | 선언 안 한 al-plan 토큰 · 관리자 토큰 · gr-io 토큰 | 403 secret_not_declared · 403 agent_only · 200(no-store) | P0 |
| E15-04 | 비밀 회전 | `POST …/secrets/guard-secret/rotate` | 새 버전·이전 버전 보존, 에이전트가 갱신 주기 안에 반영, 값 일치(해시) | P0 |
| E15-05 | 감사 연계 (SEC-08 → IAM-08) | 조직 감사 로그 | secret.read success(gr-io)·denied(al-plan)·secret.rotate, 값 미포함 | P0 |
| E15-06 | 공급자 키 무중단 회전 (SEC-04 · GW-KEY) | Gateway `POST /admin/upstream-keys/openai/rotate` | 무효 키 422(교체 안 함), 유효 키 → 버전+1·이전 키 retiring(24h)·backend bao, 평문 미노출 | P0 |
| E15-07 | OpenBao 운영 성질 (SEC-01·02 리허설) | 초기화·토큰·재기동 | 초기화·해제, 루트 토큰 미보관(폐기), Manager↔Gateway 정책 분리(서로의 경로 permission denied), 토큰 파일 읽기 전용, 재기동 시 봉인 → API 502 → openbao-init 재실행으로 복구 | P0 |
| E15-08 | 단위 | go test(secrets file·KV v2 fake · keyrot · broker API) · pytest(SDK 클라이언트) | 전부 PASS — 경로 순회 거부, 목록·감사에 값 없음, 회전 유예·폐기·기한 알림 | P0 |
