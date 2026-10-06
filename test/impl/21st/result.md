# 시험 결과 / Test Result — impl 21st (Epic 21 성숙도 MAT + 콘솔 M3 · M3 1차 게이트)

- 일자 Date: 2026-10-02
- 대상 Target: MAT 단위(`units/mat` — 수집기·판정 정의 `maturity.yaml`·엔진·스냅샷·마이그레이션 0010), compose `agent-mat`(8017), Manager `/svc/{bl,mat}` 허용 · 감사 검증 에이전트 허용, 콘솔 홈 진화 띠(동적)·성숙도·09 Bitter Lesson·평가(judge·스코어카드) 화면, 시험 hostshim
- 환경 Env: Docker 29.8.0 · macOS Darwin 25.6.0 · Go 1.26 · Python 3.12 · Chrome(호스트, playwright-core)

## 요약 / Summary

- 전체 Total: 12, 통과 Pass: 12, 실패 Fail: 0, 최종 Verdict: **PASS** (local round 3 · prod round 2)
- 단위: pytest 151 passed(+4) · vitest 20 passed(+1) · go test manager ok
- **M3 1차 게이트 `m3a-gate.sh`: 248 PASS · 0 FAIL (round 2)** — test-all · 레드팀 · prod migrate·db-ping · Epic 1~21 스모크 · SSO UI(v1·M2·M3) · CLI SSO · RLS · OpenBao · 하드닝 20종
- PRD M3 종료 게이트 측정: **MAT L4 도달**(E21-02, prod) · **모델 교체 자동화**(E20-01~04: 후보 0% → 회귀 평가 → 카나리 → 전환 → 롤백, prod)

## 케이스 / Cases

| id | 결과 | 비고 |
|----|------|------|
| E21-01 | PASS | 4속성 ≥ 70 · 출처 오류 없음 |
| E21-02 | PASS | L4 Autonomous Multi-agent |
| E21-03 | PASS | 10개념 커버리지 |
| E21-04 | PASS (gate r2) | 승격 루프·회귀 0 |
| E21-05 | PASS | 로드맵 0 카드 |
| E21-06 | PASS | 공통 지표 7종 |
| E21-07 | PASS | 스냅샷 2 · 리포트 |
| E21-08 | PASS | 띠 L4 100% |
| E21-09 | PASS | 성과 3카드 · 기준선 |
| E21-10 | PASS | 단계 변경 이벤트 |
| E21-11 | PASS | mat.assess skill |
| E21-12 | PASS | 콘솔 M3 E2E 5/5 (local·prod) |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 단위 | 수집 실패 | 빈 env(`ATLAS_MANAGER_URL=""`)를 그대로 URL 로 | 빈 값이면 기본 URL | PASS |
| 단위 | 기준선 시각 불일치 | SQLite 가 tz 를 버림 | 스냅샷 뷰 UTC 보정 | PASS |
| local 1 | 진단 500 | EV(SQLite) 실행 시각 tz 없음 → 비교 오류 | tz 없으면 UTC 가정 | local 2 |
| local 2 | trace 승격 409 | 긴 시스템 프롬프트 요청은 본문 캡처 상한으로 미수집(정상) | 승격용 trace 는 짧은 요청으로 | local 3 PASS |
| UI | Cost-aware 막대 나머지가 초록 | 기존 `.bar`(워터폴) 클래스와 충돌 | `.meter` 로 분리 | PASS |
| prod 1 | 감사 무결성 unknown → 단계 L0 | 감사 검증이 auditor 권한 — MAT 에이전트 주체 403 | 같은 조직 에이전트에 **검증 결과만** 허용(감사 목록은 auditor 그대로, Go 시험 추가) | prod 2 PASS |
| gate 1 | E21-04 판정 hold | 게이트 부하 중 기준·후보 실행 p95 차이가 허용폭(×1.3 · +50ms) 초과 — 채택 규칙이 의도대로 보수적으로 판단 | 시험은 판정 존재·회귀 0 만 확인(성숙도 게이트와 같은 조건) | gate 2 PASS |
| 환경 | 호스트 8080 점유(다른 프로젝트 컨테이너) | 사용자 작업 보호 — 중지하지 않음 | `MANAGER_PORT=18080` + 시험 전용 hostshim(`test/impl/21st/hostshim`: urllib·httpx·subprocess·curl 의 8080 URL 재작성, authshim 연동) · UI 는 콘솔 :5173 | gate PASS |

## 설계 메모

- 판정은 전부 `maturity.yaml` 의 식(안전 평가기)이고 사실이 없으면 unknown(통과로 치지 않음) — 출처 장애는 `source_errors` 로 드러난다.
- 단계는 연속 게이트: 낮은 단계 게이트가 하나라도 미달이면 위 단계 게이트를 모두 충족해도 오르지 않는다(콘솔은 단계별 충족 표시).
- 사실은 플랫폼 전역 텔레메트리(조직 문맥) — 프로젝트는 스냅샷 이력·롤업 단위.
- 홈 진화 띠는 MAT `/v1/band` 를 30초마다 읽어 현재 위치 마커·진척률·다음 단계 미달 칩을 그린다(실패 시 정적 띠).

## 가정 / 후속

- MAT-07(추가 속성)·MAT-11(자가 진단 설문)은 P2 제외. PDF 리포트 대신 Markdown(경영진용 변환은 외부 도구).
- 외부 이슈 트래커 export 는 GitHub Issues 형식 JSON 생성까지(실제 게시 없음).

## 첨부 / Logs

`logs/` (비커밋): local-all · E21-local(.r2·.r3) · E21-prod(.r2) · ui-local · ui-prod · m3a-gate(.r2) · gate-*
