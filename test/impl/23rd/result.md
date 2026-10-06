# 시험 결과 / Test Result — impl 23rd (Epic 23 샌드박스·MCP 시스템·리소스)

- 일자 Date: 2026-10-02
- 대상 Target: X-SBX 단위(`units/sbx` — sbx.run_code·상한·격리 수준·egress 정책·산출물 비밀/PII 검사·실행 기록, 마이그레이션 0012), `sandbox`(Python·Node·sh, out/ 산출물, 프록시 송신), `egress-proxy`(CONNECT·HTTP, 정책별 허용목록, 사설/메타데이터 차단, 결정 로그), MCP-SYS 단위(`units/mcp_sys` — `/connectors/mcp` 커넥터 팩·자격증명 브로커·ERP 읽기 전용·리소스·완성·SSE 변경 알림·프로젝트 경계·마스킹), `mcp-sbx`(stdio MCP 격리 브리지), MCP-REG 헬스 서킷·stdio 패키지 URL, MCP-HOST 리소스 캐시·`x-atlas-project`, SDK `mounts`
- 환경 Env: Docker 29.8.0 · macOS Darwin 25.6.0 · Python 3.12 · PostgreSQL 17 · 호스트 8080 점유 → `MANAGER_PORT=18080`

## 요약 / Summary

- 전체 Total: 15, 통과 Pass: 15, 실패 Fail: 0, 최종 Verdict: **PASS** (local round 4 · prod round 1)
- 단위: pytest 168 passed(+10: test_sbx 4 · test_mcp_sys 6) · ruff clean

## 케이스 / Cases

| id | 결과 | 비고 |
|----|------|------|
| E23-01 | PASS | Python · 산출물 report.txt · 기록 |
| E23-02 | PASS | JavaScript · 셸 exit 3 |
| E23-03 | PASS | 기본 차단 · 허용 200 · not_in_allowlist · metadata_address |
| E23-04 | PASS | 시간·메모리 limit_exceeded · 상한 422 · gvisor 422 |
| E23-05 | PASS | AWS 키 REDACTED · 전화 마스킹 |
| E23-06 | PASS | A2A sbx.run_code |
| E23-07 | PASS | Bearer 주입 · 결과에 토큰 없음 |
| E23-08 | PASS | web_fetch 허용 · 외부·메타데이터 차단 |
| E23-09 | PASS | 이름 질의 · 임의 SQL 거부 · prod `atlas_erp_ro` SELECT only |
| E23-10 | PASS | 템플릿·완성·기밀 해시 요약·-32002·행 필터 |
| E23-11 | PASS | SSE resources/updated |
| E23-12 | PASS | connectors(official)·stdio-demo(community → mcp-sbx) |
| E23-13 | PASS | 비루트 · env 비밀 없음 · 프로필 egress · 읽기 전용 FS |
| E23-14 | PASS | closed → open(노출 제외) → closed · 이벤트 |
| E23-15 | PASS | miss→hit · etag · ttl 30s · 프로젝트 분리 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 단위 | 커넥터 시험이 실제 DNS 조회 | `proxy=` 를 주면 httpx 가 transport 를 무시 | 시험 transport 가 있으면 프록시 생략 | PASS |
| local 1 | 산출물 없음 | `make local-all` 이 기존 `sandbox` 이미지를 재빌드하지 않음 | `dc.sh local up -d --build sandbox` | PASS |
| local 2 | stdio-demo 도구 0개(`Unknown tool`) | 자식이 `-I` 격리 모드라 PYTHONPATH 무시 → 모듈 못 찾음 | LIMITER 가 번들 경로를 sys.path 에 넣고 runpy 로 실행 | PASS |
| local 3 | stdio 자식이 root | dev 이미지 기본 사용자 root | `mcp-sbx` 에 전용 UID `61001` 고정 | PASS |

## 설계 메모

- egress: 실행/서버마다 프록시 정책(id·토큰·허용목록·TTL)을 등록하고 자식에 `HTTP(S)_PROXY=http://<id>:<token>@egress-proxy:3128` 만 준다. 사설 주소는 정확한 호스트명으로만 열리고 와일드카드·메타데이터 주소는 항상 거부.
- 커넥터 토큰은 Manager 비밀 중개로만(401 이면 새 값으로 1회 재시도) — 환경변수 비밀 차단.
- ERP: 이름 질의 화이트리스트 + 읽기 전용 트랜잭션(PostgreSQL `SET LOCAL ROLE atlas_erp_ro`, SQLite `PRAGMA query_only`). 쓰기는 `erp_create_order`(승인 대상) 별도.
- 리소스 캐시 키 = (tenant, project, server, uri), TTL 은 서버 메타 `ttlMs`·`cacheScope`.

## 가정 / 후속

- 격리 수준은 `container` 만 제공(gVisor runsc·Firecracker 미가용) — 상위 수준 요청은 422. X-SBX-04(브라우저·데스크톱 샌드박스)는 후속(계획표 가정).
- 커넥터 업스트림은 가짜(`fake-upstream`) — 실 GitHub·Slack·Jira 는 `upstreams` 설정과 비밀만 바꾸면 된다.
- 레지스트리는 도구 0개 서버도 등록된다(리소스 전용 서버 허용 목적) — 브리지 장애 시 pins 가 비므로 재등록 필요.

## 첨부 / Logs

`logs/e23-local-r1..r4.txt` · `logs/e23-prod-r1.txt` · `logs/local-build.txt` · `logs/prod-build.txt`

## 후속 변경 (impl 24th 게이트에서)

- stdio 시연 서버 레지스트리 이름 `io.prodai/stdio-demo` → `io.prodai/stdio-echo` (기존 E7-03 의 검색어 "demo" 와 충돌). MCP-REG 기본 시드는 데모·Manager API 2종 그대로, 커넥터·stdio 는 `seed --systems` (E23-12 가 사용).
