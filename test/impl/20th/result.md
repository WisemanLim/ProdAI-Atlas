# 시험 결과 / Test Result — impl 20th (Epic 20 Bitter Lesson)

- 일자 Date: 2026-10-02
- 대상 Target: Gateway `internal/aliases`(오버레이·버전·롤백·서빙 통계)·라우터(오버레이·weight 0·고정 배정)·`econ/compute.go`(계산 정책·난이도·추론 예산)·mock(군말 플래너·시드 다양성·prose 지시)·exact 캐시 키, Manager `POST /api/v1/events`·`gateway-admin-token` 이관, 런타임 `atlas_agent_core.flags`(OFREP)·`prompts`(레지스트리·하네스·버전 고정)·`events`, BL 단위(`units/bl`), AL-PLAN 보정 플래그·프롬프트 레지스트리, AL-RUN 하네스, EV 타깃 `effort`·`metadata`, compose `agent-bl`(8015)·`flagd`, 마이그레이션 0009
- 환경 Env: Docker 29.8.0 · macOS Darwin 25.6.0 · Go 1.26 · Python 3.12 · flagd v0.12.9

## 요약 / Summary

- 전체 Total: 12, 통과 Pass: 12, 실패 Fail: 0, 최종 Verdict: **PASS** (local round 3 · prod round 1)
- 단위: go test gateway ok(+2: alias 카나리·계산 정책) · pytest 147 passed(+8)

## 케이스 / Cases

| id | 결과 | 비고 |
|----|------|------|
| E20-01 | PASS | promote · 후보 0% · 임시 alias 정리 |
| E20-02 | PASS | 고정 배정 · 승격 + `-prev` 폴백 |
| E20-03 | PASS | 롤백 · 감사 4종 |
| E20-04 | PASS | hold · 비용 SLO 롤백 |
| E20-05 | PASS | EOL 대체 mock-agent · 모델 카드 |
| E20-06 | PASS | auto 난이도 · high→medium 하향 |
| E20-07 | PASS | 게시 게이트 pass/409 |
| E20-08 | PASS | flagd 로 보정 on/off |
| E20-09 | PASS | ablation removable·keep |
| E20-10 | PASS | 하네스 턴 한도 · 규칙 require_approval |
| E20-11 | PASS | best-of-n · effort low 추천 |
| E20-12 | PASS | plt.bl.* PLT 스트림 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 단위 | 라우터 시험 3건 실패 | 시험이 weight 없이 설정을 만든다 — weight 0 을 "트래픽 없음"으로 바꾼 뒤 배포 제외 | `router.New` 에서 설정 alias weight 0 → 1 정규화(0 은 오버레이 전용) | PASS |
| 단위 | 난이도 hard 판정 안 됨 | 키워드 가점 상한 2 | 상한 3 | PASS |
| 설계 검토 | **effort 가 달라도 exact 캐시 적중** | exact 캐시 키에 최종 effort·max_tokens 미포함 — 추론 깊이가 다른 요청이 같은 응답을 받음(기존 결함) | 키에 계획 effort·max_tokens 포함 | 곡선 시험 PASS |
| local 1 | Manager 8080 바인드 실패 | 호스트 8080 을 다른 프로젝트 컨테이너가 사용 | `MANAGER_PORT=18080` 우회 + 시험 `ATLAS_MANAGER_HOST_URL`(gatekey 포함, 기본값 8080 유지) | — |
| local 1 | BL `/v1/models` 500 | `config.Deployment` JSON 태그 없음(대문자 키) | json 태그 + fallbacks `[]` | local 2 |
| local 2 | EOL 항목 없음 | 카탈로그 필드명 `deprecation_in_days`·`prices_per_1m` | BL 이 정규화 · 대체 후보 기대값을 규칙상 최저가(mock-agent)로 | local 3 PASS |

## 설계 메모

- alias 오버레이 정본은 Redis — 각 Gateway 가 2초마다 동기화. 변경은 버전·이력(최근 200), 롤백은 이력 내용을 새 버전으로. 서빙 통계는 (alias, 버전, 모델) 단위라 카나리 단계마다 새로 쌓인다.
- 채택 규칙은 안전한 식 부분집합(`units/bl/expr.py`, 속성 접근·임의 호출 불가). 기본: 품질 −1pp 이내 · 비용 ≤1.2× · p95 ≤ max(1.3×, +50ms) → promote, 품질 +5pp 초과 → review-cost(유의하지 않으면 insufficient-evidence), 그 외 hold.
- 보정 플래그 정본은 BL DB, flagd 파일은 BL 데이터 볼륨에 원자적 기록(flagd 는 같은 볼륨 읽기 전용). 에이전트는 OFREP(5초 캐시, 실패 시 기본값). ablation·게시 게이트는 A2A 메시지 metadata `atlas.flags`·`atlas.prompts`·`atlas.harness` 로 요청 단위 덮어쓰기(`harness.*` 만).
- 런타임 이벤트는 Manager `POST /api/v1/events` 로 중계(`plt.bl.*`·`plt.ev.*`·`plt.mat.*` 만, 조직은 주체 조직) — X-NTF 알림 규칙 대상.

## 가정 / 후속

- LiteLLM 가격표 주기 동기화(MODEL-01)는 외부 네트워크 금지로 미구현 — 카탈로그 정본은 Gateway 설정. GrowthBook 연동 대신 내장 통계. CEL 대신 내장 식 평가기.
- BL-MODEL-09·HARN-07·08·COMP-04·07 은 P2 제외.

## 첨부 / Logs

`logs/` (비커밋): local-all(.r2) · E20-local(.r2·.r3) · E20-prod · prod-build
