# 시험 시나리오 / Test Scenario — impl 26th (Epic 26 MCP 고급)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` MCP-HOST-03·04·05·07·08·10 · MCP-REG-04·07·08 · MCP-TOOL-06 · MCP-SEL-04
- 범위 결정 (2026-10-02, 사용자): 잔여 P1 감사 후 Epic 25~
- 실행: `e26_mcp.py` 가 실제 MCP-REG(8002)·MCP 게이트웨이(`agent-mcp-gw` 8024 `/t/{tenant}/mcp/{vs}`)·`mcp-sbx`(stdio `io.prodai/interactive`)·OAuth 보호 서버(`mcp-protected`, Keycloak 토큰 검증)·Keycloak(서비스 계정 atlas-ci)·GR-IO·Manager(게이트웨이 토큰 aud=atlas-mcp-gateway)·NATS 를 호출. 프로토콜 2026-07-28(MRTR) 기준.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E26-01 | 서명·출처·메타 (REG-04·08) | 서명 없는 stdio 패키지를 verified 승인 · 게시자 키 서명본 | 409 verify_failed · signature·provenance valid · owners·transport | P0 |
| E26-02 | 채널 고정·승격·롤백 (REG-07) | prod 직행 · dev→staging→prod · rollback | 409 · 이전 버전 복귀 · 이력 | P1 |
| E26-03 | OAuth 보호 서버 등록 (HOST-03) | 인증 없는 호출 · 레지스트리 스캔 | 401 + resource_metadata · 서비스 계정으로 scanned | P0 |
| E26-04 | 게이트웨이 단일 엔드포인트 (HOST-04) | tools/list | `{별칭}__{도구}` · 결정적 정렬 · 4개 서버 도구 | P0 |
| E26-05 | 업스트림 토큰 분리 (HOST-03·07) | protected_hr__whoami | client_id = atlas-ci · 인바운드 토큰 미포함 | P0 |
| E26-06 | 입력 요청 중계 (HOST-05) | confirm_delete (MRTR, mcp-sbx 경유) | 클라이언트 elicitation 응답이 stdio 서버까지 | P0 |
| E26-07 | 진행률 중계 (HOST-05) | long_task 3단계 | 1/3·2/3·3/3 | P1 |
| E26-08 | 결과 크기 제한 (TOOL-06) | big_report 3000줄 | 잘림 표시 · 상한 이내 | P1 |
| E26-09 | 가드 훅·오염 (HOST-08) | read_inbox(간접 인젝션) → send_email · 다른 실행 | untrusted · 같은 실행 외부 쓰기 차단 · 다른 실행 정상 | P0 |
| E26-10 | toolset 가상 서버 (SEL-04) | ops-readonly 프로필 | 읽기 도구만 | P1 |
| E26-11 | Origin·토큰 대상 (HOST-07·10) | 악성 Origin · aud=atlas-manager 토큰 · 다른 조직 경로 · 게이트웨이 토큰으로 Manager | 403 · 401 · 403 · (prod) 401 | P0 |
| E26-12 | 이벤트 | NATS PLT | plt.mcp.reg.verify_failed · channel_changed | P1 |
| 단위 | pytest test_mcp_gw(5) · test_mcp_oauth(3) · go test 게이트웨이 토큰 대상 | — | PASS | P0 |
