# 시험 결과 / Test Result — impl 7th · Epic 7 MCP 핵심

- 일자 Date: 2026-10-02
- 대상 Target: `units/mcp_reg`(레지스트리·스캔·고정·허용목록, compose `agent-mcp-reg` :8002) · `units/mcp_host`(클라이언트 풀·고정 검사·승인 규칙·감사 span) · `units/mcp_sel`(BM25·메타 도구) · `mcp_servers/demo`(:8003) · `mcp_servers/openapi_tools`(MCP-TOOL, :8004) · AL-RUN 연동 · Manager OpenAPI operationId · alembic 0003 · `make mcp-seed`
- 의존성: fastmcp 4.0 (MCP 2026-07-28 · server/discover 지원)
- 커밋 Commit: — (git 저장소 아님)

## 요약 / Summary

- 전체 Total: 12, 통과 Pass: 12, 실패 Fail: 0, 최종 Verdict: **PASS** (round 2 — 시험 코드 1건 수정)
- 구현 중 결함 2건 수정: 스캐너 소스에 실제 제어 문자가 들어간 문제, 데모 서버의 조건식 독스트링 미적용(오염 설명이 반영되지 않음).

## 케이스 / Cases

| id | 결과 result | 비고 note |
|----|-------------|-----------|
| E7-01 | PASS (round 2) | 컨테이너 pytest 60 passed · 1 skipped(OpenAPI 명세 파일이 컨테이너에 없음 → E7-06 실서버로 대체 검증), 호스트 61 passed |
| E7-02 | PASS | demo(pins 4)·manager-api(pins 6) scanned → approved, default 허용목록 |
| E7-03 | PASS | `/v0.1/servers?search=demo` 1건(server + _meta status approved), versions 엔드포인트 pins 4 |
| E7-04 | PASS | default: 2개 · `shadow-team`(허용목록 없음): 0개 |
| E7-05 | PASS | loop.run 이 `demo__search_docs` 호출, 출력에 문서 내용, 이벤트 mcp(tools 10)·tools_selected |
| E7-06 | PASS | `manager_api__listCatalogUnits`(OpenAPI→MCP) 결과에 AL-PLAN |
| E7-07 | PASS | `demo__delete_records`(destructiveHint·verified) → INPUT_REQUIRED |
| E7-08 | PASS | DEMO_POISON 재기동 → `demo__search_docs` pin_mismatch 차단·미선별 |
| E7-09 | PASS | 오염 서버 등록 rejected, score 100 (instruction_tag·override_phrase·secret_path) |
| E7-10 | PASS | `mcp.tools.call demo__search_docs` span 이 invoke_agent al-run 하위, protocol 2026-07-28 |
| E7-11 | PASS | prod 리허설에서 seed + E7-03~10 동일 |
| E7-12 | PASS | `make test-all` exit 0 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 bug | 원인 cause | 수정 fix | 재시험 retest |
|-------|----------|------------|----------|---------------|
| 0 (구현 중) | ruff PLE2502 — scan.py 에 보이지 않는 제어 문자 | 정규식 범위를 리터럴 문자로 기록(소스 난독화 위험 — 스캐너가 잡아야 할 패턴과 같음) | `\uXXXX` 이스케이프 표기로 교체 | 단위 시험 hidden_unicode 탐지 확인 |
| 0 (구현 중) | 오염 모드에서도 도구 설명이 그대로 | 조건식을 독스트링 자리에 둬서 `__doc__` 로 쓰이지 않음 (ruff B018) | `description=` 인자로 지정 | E7-08·09 통과 |
| 1 (시험 코드) | 컨테이너에서 `test_openapi_to_mcp_tools` IndexError | `parents[3]` 가정 — 컨테이너 경로가 얕음 | 상위 디렉터리 탐색 후 없으면 skip | round 2 통과 |

## 참고 (M1 1차 범위 밖)

- OAuth 2.1(HOST-03)·게이트웨이 모드 단일 엔드포인트(HOST-04)·양방향 중계(HOST-05)·stdio 서버·서명 검증(REG-04)·임베딩 하이브리드 검색·LLM 판정 스캔.
- MCP-SEL 은 도구 이름 토큰(서버 별칭)도 색인하므로 별칭이 목표에 나오면 같은 서버 도구가 함께 선별됨 — 사용 통계·임베딩 도입 시 보정.

## 첨부 / Logs

`logs/` (비커밋) · 재실행: `make mcp-seed`, `e7_mcp.py <local|prod>`, `e7-rugpull.sh <mode> <reg_admin_token>`, `e7-prod.sh`
