# 시험 결과 / Test Result — impl 19th (Epic 19 Judge·점수)

- 일자 Date: 2026-10-02
- 대상 Target: Gateway `provider/judge.go`(mock judge: binary·pairwise·claim, 변형 lenient·length·biased)·alias `judge-*`, EV `units/ev/judge.py`·`judging.py`·`stats.py`·스위트 `judge` 채점기·마이그레이션 0008(`ev_judges`·`ev_scores`·`ev_online_rules`), GR-IO 비밀 조회 비동기화
- 환경 Env: Docker 29.8.0 · macOS Darwin 25.6.0 · Go 1.26 · Python 3.12

## 요약 / Summary

- 전체 Total: 12, 통과 Pass: 12, 실패 Fail: 0, 최종 Verdict: **PASS** (local round 3 · prod round 1)
- 단위: pytest 139 passed(+4) · go test gateway ok(+1)

## 케이스 / Cases

| id | 결과 | 비고 |
|----|------|------|
| E19-01 | PASS | v1·v2 · pass/fail · critique |
| E19-02 | PASS | B 일관 · 편향 judge tie |
| E19-03 | PASS | 배심원 다수결 0.6667 |
| E19-04 | PASS | κ 1.0 trusted · 길이 judge untrusted |
| E19-05 | PASS | v1 0.0 fail · v2 1.0 pass · 미보정 judge 게이트 fail |
| E19-06 | PASS | McNemar 0.0625 · CI [1,1] · 역방향 regressed |
| E19-07 | PASS | v3 pass→fail 5/10 |
| E19-08 | PASS | faithfulness·precision·recall 0.5 |
| E19-09 | PASS | 실제 trace 3 → 저점 2 후보(PII 마스킹) |
| E19-10 | PASS | 👎 → 후보 |
| E19-11 | PASS | 추세·스코어카드 |
| E19-12 | PASS | OB 비용 EV-JUDGE > 0 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 단위 | shadow 표본 수 기대 오류 | 시험 계산 실수(미보정 judge 실행은 다른 judge) | 기대값 8 | PASS |
| 단위 | 스코어카드 기대 | 타깃별 **최신** 실행을 보여 줌(설계) | 시험이 최신 실행 기준으로 | PASS |
| local 1 | 온라인 후보 생성 500 | GR-IO 가 요청 경로에서 동기 `secrets.value` — 재기동 직후 비밀 선조회 전 요청 | `await secrets.get(...)`(없으면 즉시 중개 조회) | local 2 PASS |
| local 2 | 과업당 비용 0 | 반복 시험에서 같은 요청이 Gateway exact 캐시(temperature 0)로 비용 0 — 정상 동작 | 시험 템플릿에 실행별 nonce | local 3 PASS |

## 설계 메모

- judge 프롬프트는 `## MODE / RUBRIC / INPUT / EXPECTED / OUTPUT(A·B) / CLAIM / CONTEXT` 섹션 — 실모델에도 그대로 쓰고, mock 은 루브릭 줄 `포함:`·`금지:` 로 결정적으로 판정한다.
- 보정(κ ≥ `ATLAS_EV_JUDGE_MIN_KAPPA` 기본 0.6)되지 않은 judge 를 게이트 스위트에 쓰면 실패(`allow_uncalibrated: true` 로만 허용).
- 점수는 EV 관계형 저장소(`ev_scores`, SQLite/PostgreSQL)에 둔다 — PRD 의 ClickHouse `eval_score` 대신(가정). 추세·diff·스코어카드는 실행·점수 테이블에서 계산.
- 온라인 표본은 trace_id 해시로 결정적 추출, 같은 trace 재채점 금지, 저점·👎 은 `online-candidates`(또는 규칙 dataset) 에 in_review 로 — 마스킹은 기존 trace 승격 경로(GR-IO retrieval) 재사용.

## 가정 / 후속

- EV-JUDGE-07(편향 완화 옵션)·10(비용 캐스케이드)·EV-SCORE-06(포화 감시)은 P2 제외. 드리프트 경보 이벤트 발행·콘솔 화면은 Epic 21.

## 첨부 / Logs

`logs/` (비커밋): local-all · E19-local(.r2·.r3) · E19-prod · prod-build
