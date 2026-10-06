# 시험 시나리오 / Test Scenario — impl 23rd (Epic 23 샌드박스·MCP 시스템·리소스)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` X-SBX-01·02·03·06·07 · MCP-SYS-01~06 · MCP-RES-01~06
- 범위 결정 (2026-10-02, 사용자): M3 2차 Epic 22~24 · 커넥터 = 로컬 가짜 업스트림(`fake-upstream`) + Postgres 읽기 전용 커넥터 실연동
- 실행: `e23_sbx_mcp.py` 가 실제 X-SBX(`agent-sbx` 8019)·`sandbox`·`egress-proxy`·MCP-SYS(`agent-mcp-sys` 8020 `/connectors/mcp`)·`mcp-sbx`(내부망)·MCP-REG(8002)·MCP-HOST(`agent-al-run` 컨테이너 안)·GR-IO·Manager 비밀 중개·NATS 를 호출. prod 는 PostgreSQL `atlas_erp_ro` 역할까지.
- 커넥터 자격증명은 Manager `PUT /api/v1/orgs/default/secrets/{github,slack,jira}-token` 으로 넣는다(시험 값 = 가짜 업스트림 토큰). egress 관리 토큰은 `ATLAS_EGRESS_ADMIN_TOKEN` → Manager 1회 이관(`egress-admin-token`).

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E23-01 | Python 실행·산출물·실행 기록 (SBX-01·05) | `out/report.txt` 쓰기 + print | ok · stdout 45 · 산출물 1 · 기록(격리 수준·코드 해시) | P0 |
| E23-02 | JavaScript·셸 (SBX-01) | node map · `exit 3` | ok "2,4,6" · error exit 3 | P0 |
| E23-03 | 네트워크 기본 차단·허용목록 (SBX-02) | 정책 없음 · allow fake-upstream·메타데이터 | 차단 · 허용 200 · 허용 밖 not_in_allowlist · metadata_address 거부 · 결정 기록 | P0 |
| E23-04 | 자원 상한·격리 수준 (SBX-02·03) | 무한 루프 2초 · 400MB/128MB · 상한 초과 · gvisor | limit_exceeded ×2 · 422 · 422 · 사용 가능 [container] | P0 |
| E23-05 | 산출물 비밀·PII 검사 (SBX-06) | AWS 키 · 전화번호 출력 | 키 [REDACTED] · 전화 마스킹 · findings | P0 |
| E23-06 | A2A skill sbx.run_code | SendMessage data | COMPLETED · 결과 42 | P1 |
| E23-07 | 커넥터 팩·자격증명 브로커 (SYS-01·02) | GitHub 목록·생성 · Slack | 성공 · 업스트림에 Bearer 주입 · 결과에 토큰 없음 | P0 |
| E23-08 | egress·SSRF (SYS-04) | web_fetch 허용·외부·메타데이터 | 200 · 차단 · 차단 | P0 |
| E23-09 | DB 읽기 전용 커넥터 (SYS-05) | 이름 질의 · 임의 SQL · 경계 kr-sales · (prod) 역할 권한 | KR 행만 · 거부 · (prod) SELECT=true INSERT=false | P0 |
| E23-10 | 리소스·템플릿·완성·경계·마스킹 (RES-01·02·05·06) | 템플릿 목록 · 완성 erp_c · 기밀 테이블 · private 문서 · kr-sales | 템플릿 2 · erp_customers · 기밀 = 해시 요약(PII 없음) · -32002 경계 밖 · 행 필터 | P0 |
| E23-11 | 변경 알림 (RES-03) | SSE 구독 중 주문 생성 | subscribed → resources/updated | P1 |
| E23-12 | 레지스트리 등록 (SYS-03·SBX-07) | `units.mcp_reg.seed` | connectors(official)·stdio-demo(community, mcp-sbx URL) 허용 · health 도구 고정 | P0 |
| E23-13 | stdio 격리 실행 (SBX-07) | whoami · fetch 허용/외부 · /app 쓰기 | 비루트 · 비밀 env 없음 · 허용 OK · 외부 차단 · 쓰기 실패 | P0 |
| E23-14 | 헬스·SLA 서킷 (SYS-06) | 업스트림 장애 3회 → 복구 | closed → open(유효 목록에서 제외) → closed · plt.mcp.sys.health · plt.sbx.limit_exceeded | P0 |
| E23-15 | 호스트 리소스 캐시 (RES-04) | 같은 URI 2회 · 다른 프로젝트 | miss → hit(etag 같음, ttl 30s) · 프로젝트 분리 · 경계 거부 유지 | P1 |
| 단위 | pytest test_sbx(4)·test_mcp_sys(6) | — | PASS | P0 |
