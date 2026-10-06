# 시험 결과 / Test Result — impl 26th (Epic 26 MCP 고급)

- 일자 Date: 2026-10-02
- 대상 Target: MCP 게이트웨이 단위(`units/mcp_gw`, unitId MCP-HOST, 8024 — `/t/{tenant}/mcp/{vs}`·가상 서버·toolset·MRTR/진행률 중계·Origin·aud 토큰), MCP-HOST 라이브러리(OAuth 2.1 서비스 계정 `oauth.py`·가드 훅·오염 차단·결과 크기·toolset 필터·결정적 정렬), MCP-REG(서명·출처 검증·신뢰 메타·채널·게시자 키·OAuth 스캔/헬스·이름당 1버전), mcp-sbx(`create_proxy(transport)` — 진행률·입력 요청 중계), 시험 서버 `mcp-protected`(Keycloak 토큰 검증)·`interactive`(stdio), Manager `?audience=atlas-mcp-gateway`, 마이그레이션 0014
- 환경 Env: Docker 29.8.0 · macOS Darwin 25.6.0 · FastMCP 4.0.10 (MCP 2026-07-28) · Keycloak · PostgreSQL 17

## 요약 / Summary

- 전체 Total: 12, 통과 Pass: 12, 실패 Fail: 0, 최종 Verdict: **PASS** (local round 3 · prod round 1)
- 단위: pytest 190 passed(+8: test_mcp_gw 5 · test_mcp_oauth 3) · go test ok(+1 게이트웨이 토큰 대상) · E23 local 회귀 15/15

## 케이스 / Cases

| id | local | prod | 비고 |
|----|-------|------|------|
| E26-01 | PASS | PASS | 서명 없는 패키지 verified 거부 · 서명·provenance valid |
| E26-02 | PASS | PASS | prod 직행 409 · 승격·롤백 |
| E26-03 | PASS | PASS | 401 + resource_metadata · 서비스 계정 스캔 |
| E26-04 | PASS | PASS | 단일 엔드포인트 · 정렬 · 4개 서버 |
| E26-05 | PASS | PASS | 업스트림 client_id=atlas-ci · 인바운드 토큰 비전달 |
| E26-06 | PASS | PASS | MRTR 입력 요청 (게이트웨이 → mcp-sbx → stdio) |
| E26-07 | PASS | PASS | 진행률 3단계 |
| E26-08 | PASS | PASS | 결과 잘림 |
| E26-09 | PASS | PASS | 간접 인젝션 → 오염 → 외부 쓰기 차단 · 다른 실행 정상 |
| E26-10 | PASS | PASS | ops-readonly 가상 서버 |
| E26-11 | PASS | PASS | Origin 403 · aud 401 · 조직 403 · (prod) Manager 401 |
| E26-12 | PASS | PASS | verify_failed · channel_changed |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 단위 | 게이트웨이 404 | Starlette Mount 는 전체 경로 + root_path 를 넘김 | root_path 를 떼고 경로 판정 | PASS |
| 단위 | 입력 요청 실패 (`elicitation via server-initiated requests is unavailable on 2026-07-28`) | 2026 프로토콜은 서버 발 elicitation 금지 — MRTR(SEP-2322) | 업스트림 `InputRequiredResult` 를 그대로 하위로(`InputRequiredToolResult`), 답(`input_responses`·`request_state`)을 업스트림에 재발행 · 시험 서버를 guard 패턴으로 | PASS |
| 단위 | 진행률이 하위로 안 감 | 업스트림 수신 태스크에는 하위 요청 문맥이 없어 알림이 버려짐 | 큐에 넣고 요청 태스크가 전달 | PASS |
| 단위 | 오염 표시가 다음 호출에 없음 | 2026 은 무상태 — 요청마다 세션 id 가 다름 | 실행 id(`x-atlas-run-id`) 단위, 없으면 주체 단위 + TTL | PASS |
| 단위 | 서명 검증 invalid | 레지스트리가 기본값(`remotes: []`)을 채운 문서로 해시 | 서명 대상 = 정규화 server.json | PASS |
| local 1 | stdio 경유 진행률 없음 | 브리지가 `create_proxy(Client(...))` — 일반 Client 는 문맥 복원 핸들러가 없음 | `create_proxy(transport)` (ProxyClient) | PASS |
| local 1 | 간접 인젝션이 오염으로 안 잡힘 | GR tool_result 경계는 jailbreak monitor(차단 아님) — 카테고리 이름도 다름 | jailbreak 단계 flagged 를 오염으로 · GR 스포트라이트 변환본을 돌려줌 | PASS |
| local 2 | 채널 시험 상태 누적 | 같은 버전 재사용 | 실행마다 새 버전 | PASS |

## 설계 메모

- 레지스트리 정책: 패키지(stdio·OCI) 서버를 verified/official 로 올리려면 유효 서명 필수 — 서명 없으면 community 까지. 잘못된 서명은 등급과 무관하게 거부.
- OAuth: 401 → RFC 9728 PRM → OIDC Discovery(내부 주소 치환 `ATLAS_OIDC_ISSUER_INTERNAL`) → client_credentials + resource → (tenant, AS, resource) 캐시, issuer 불일치 거부. 레지스트리 스캔·헬스도 일반 연결 실패 후에만 같은 흐름(평소 프로브 없음 — stdio 브리지 세션 남발 방지).
- 가드 훅은 `ATLAS_MCP_GUARD_HOOKS=true` 인 곳(게이트웨이)에서 켠다. AL-RUN 루프는 기존 GR-POL 경로 유지(회귀 방지) — 후속에서 켤지 결정.

## 가정 / 후속

- 사용자 위임 OAuth(Authorization Code + PKCE, 콘솔 동의 화면)·토큰 볼트 영속화·증분 scope 상향은 후속(서비스 계정만).
- list_changed 알림 중계·2025 하위 세션의 서버 발 elicitation 중계는 부분(2026 MRTR 경로가 기본).

## 첨부 / Logs

`logs/e26-local-r1..r3.txt` · `logs/e26-prod-r1.txt`
