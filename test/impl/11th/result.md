# 시험 결과 / Test Result — impl 11th · Epic 11 콘솔 v1

- 일자 Date: 2026-10-02
- 대상 Target: 콘솔 v1(`apps/console`: 셸·스위처·섹션 7개, `lib/router`·`lib/waterfall`) · Manager `internal/ui`(embed.FS·SPA fallback)·`/svc/{name}/*` 프록시·`Dockerfile.bundle`(콘솔 내장 단일 바이너리) · AL-RUN `GET /runs` · EV `GET /v1/runs` · UI E2E `apps/console/e2e/console.e2e.mjs`(playwright-core + 호스트 Chrome)
- 커밋 Commit: — (git 저장소 아님)

## 요약 / Summary

- 전체 Total: 14, 통과 Pass: 14, 실패 Fail: 0, 최종 Verdict: **PASS** (round 2)
- 시험으로 잡은 결함 3건: (1) traceparent 없는 요청의 trace 분리(고아 trace), (2) 콘솔 폴링 GET 이 trace 저장소를 오염, (3) **OpenAPI→MCP 가 새 `/svc` 프록시를 에이전트 도구로 노출**(보안).

## 케이스 / Cases

| id | 결과 result | 비고 note |
|----|-------------|-----------|
| E11-01 | PASS | manager(svc 프록시·SPA fallback·OpenAPI 정합), console vitest 13, agent pytest 113 |
| E11-02 | PASS | prod :8080 `/` 번들 HTML(해시 자산), 딥링크 SPA fallback, `/assets/*` immutable 캐시, `/api/v1/nope` JSON 404 |
| E11-03 | PASS | `/svc/{ob,alrun,ev,gr,mcpreg,gw}` 200, 허용 목록 밖 404 |
| E11-04 | PASS | 섹션 7개 렌더 (local :5173 · prod :8080), 스크린샷 `logs/{local,prod}/*.png` |
| E11-05 | PASS | 개요 맵 07 카드 → /guard |
| E11-06 | PASS | 즉석 검사 transform (주민번호·전화) |
| E11-07 | PASS | UI 에서 런 실행 → /runs/{id} · 이벤트 타임라인 |
| E11-08 | PASS | 비가역 목표 → 승인 그룹 → 승인 → END_TURN |
| E11-09 | PASS (round 2) | DSL 검색 → trace 선택 → 워터폴 14~19행·위임 그래프 |
| E11-10 | PASS | 환경 prod → 확인 모달 → 색 띠·URL `/e/prod/` |
| E11-11 | PASS | prod 에서 실행 → 확인 모달 |
| E11-12 | PASS | 브라우저 pageerror 0 |
| E11-13 | PASS | prod 리허설(Manager 내장 콘솔)에서 E11-02~12 동일 |
| E11-14 | PASS | `make test-all` exit 0 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 bug | 원인 cause | 수정 fix | 재시험 retest |
|-------|----------|------------|----------|---------------|
| 0 (시험 코드) | 프록시 시험 panic `not http.CloseNotifier` | gin + httputil.ReverseProxy 는 httptest.ResponseRecorder 로 시험 불가 | 실제 HTTP 서버(httptest.NewServer)로 시험 | 통과 |
| 1 (**제품**) | E11-09 워터폴 1행 | traceparent 없이 온 A2A(콘솔·Manager 프록시 경유)에서 `invoke_agent` 가 HTTP span 과 다른 trace 로 분리 | `otel.extract` 가 traceparent 없으면 현재 문맥(HTTP span)을 부모로 | round 2: 14행 |
| 1 (**제품**) | trace 목록이 콘솔 폴링(`GET /runs`·`/events`)으로 오염 | FastAPI 내장 텔레메트리가 모든 GET 추적 | traceparent 없는 GET 은 추적 제외(POST·추적 요청 GET 은 유지) | 단위 시험 + 화면 확인 |
| 1 (**보안**) | MCP-TOOL 시험 실패 — `proxyServiceGet` 도구 생성 | Manager OpenAPI 에 추가한 `/svc/{name}/{path}` 가 OpenAPI→MCP 로 에이전트 도구가 됨 → 에이전트가 내부 서비스 전체 호출 가능 | MCP-TOOL 변환에서 `/svc/` 접두 제외 | 통과 |
| 1 (시험 코드) | E2E 스크립트 종료 오류 | `process.exit(boolean)` | `failed ? 1 : 0` | 통과 |

## 참고 (범위 밖·한계)

- 조직·프로젝트는 단일(default) — 스위처·URL 문맥은 동작하지만 데이터 분리는 PLT-TEN(M2). 환경(dev/staging/prod)도 현재는 표시·확인 정책만.
- `/svc` 프록시는 인증 없음 — 내부망 배치 전제, PLT-IAM(M2)에서 세션·RBAC.
- 콘솔에서 골든셋 승인·정책 게시 같은 관리 작업은 API(토큰) — 화면 편집은 후속.

## 첨부 / Logs

`logs/` (비커밋, 스크린샷 포함) · 재실행: `node apps/console/e2e/console.e2e.mjs <base> <shots>`, `e11_http.py`, `e11-prod.sh`
