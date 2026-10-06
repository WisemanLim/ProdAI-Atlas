# 시험 결과 / Test Result — impl 17th (Epic 17 Multi-agent)

- 일자 Date: 2026-10-02
- 대상 Target: `units/ma_common`·`ma_orch`·`ma_res`·`ma_cr`·`ma_anl`·`ma_merge`, `agents/sandbox`(격리 실행), compose `agent-ma-*`(8010~8014)·`sandbox`(`sandbox-net` internal), SDK `secrets.get`
- 환경 Env: Docker 29.8.0 · macOS Darwin 25.6.0 · Go 1.26 · Python 3.12

## 요약 / Summary

- 전체 Total: 12, 통과 Pass: 12, 실패 Fail: 0, 최종 Verdict: **PASS** (local round 4 · prod round 3)
- 단위: pytest 134 passed
- local 12/12 · prod(oidc) 12/12

## 케이스 / Cases

| id | 결과 | 비고 |
|----|------|------|
| E17-01 | PASS | analysis·research·review 3관점, synthesis, 보고서에 sales.mean |
| E17-02 | PASS | 명세 완전 · tools_allow=[] · orchestrator-only |
| E17-03 | PASS | simple · single · 1 |
| E17-04 | PASS | first · 부분 결과 |
| E17-05 | PASS | 샌드박스 한도 = 마감-1s → 분석 FAILED, gaps 기록 |
| E17-06 | PASS | 읽기 전용 거부 |
| E17-07 | PASS | max_depth |
| E17-08 | PASS | 출처 필수 |
| E17-09 | PASS | SARIF 2.1.0 · critical |
| E17-10 | PASS | bar · 3행 · 네트워크/메모리/시간 차단 |
| E17-11 | PASS | structural · 중복 제거 |
| E17-12 | PASS | -32051 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| local 1 | E17-01 관점 1개만 실행 | 계획이 단일 단계를 돌려줌 | 단일 kind 계획이면 관점별 분해 | PASS |
| local 1 | 기동 직후 SecretUnavailable | 비밀 선조회 전에 요청 도착 | `secrets.get`(비동기, 없으면 즉시 중개 조회) | PASS |
| local 2 | E17-05 경합 | 샌드박스 한도와 오케스트레이터 마감이 같음 | 한도 = 마감-1s, 시간 초과 = 과업 실패 | PASS |
| local 2 | E17-09 시험 오류 | list vs set 비교 | 시험 수정 | PASS |
| 단위 | 샌드박스 호스트 시험 `python` 없음 · preexec_fn | 호스트 환경 차이 | `sys.executable` · exec 래퍼(rlimit) | PASS |
| 단위 | MA-RES 첫 문단 누락 | 문단 분할 버그 | 수정 | PASS |
| prod 1 | 대기 루프 미적용 | 스크립트 sed 치환 실패 | 정규식 치환 | — |
| prod 2 | E17-01 분석 비어 있음 · E17-10 IndexError | 샌드박스 `RLIMIT_NPROC 32` 는 **UID 전체 합산** — prod 의 distroless 비루트 컨테이너(65532)와 UID 공유로 OpenBLAS 스레드 생성 실패 → pandas import 중단 | 샌드박스 전용 UID 61000 + `OPENBLAS/OMP/MKL_NUM_THREADS=1` | prod 3 PASS |
| local 3→4 | 분석 401 | 로컬 비밀 저장소에 이전 디버깅 값(`x`)이 남음(import 는 1회 이관이라 덮어쓰지 않음) · 회전 후에도 캐시(300s) 사용 | 저장소 값 회전 + ma-anl 이 401 이면 `secrets.get(fresh=True)` 로 새 값 받아 1회 재시도 | local 4 PASS |

## 설계 메모

- 오케스트레이터만 쓰기 권한(writes=orchestrator-only) — 서브에이전트는 위임 명세 `tools_allow` 에 쓰기 도구가 있으면 거부.
- 샌드박스: 내부 전용 네트워크(외부 차단), read-only rootfs + tmpfs, cap_drop ALL, no-new-privileges, 실행마다 rlimit(CPU·AS·NPROC·NOFILE·FSIZE) exec 래퍼.
- 샌드박스 토큰은 Manager 비밀 중개(`sandbox-token`, ma-anl 매니페스트 `spec.secrets[]` 선언)로만 받는다.

## 가정 / 후속

- 웹 검색·외부 저장소 리뷰는 미구현(내장 코퍼스·로컬 diff, 사용자 결정). 위임 트리 콘솔 시각화는 P1.

## 첨부 / Logs

`logs/` (비커밋): local-all(.r4) · E17-local(.r2·.r3·.r4) · E17-prod(.r2) · prod-run-r3 · E17-prod-debug · prod-build
