# 시험 시나리오 / Test Scenario — impl 13th (Epic 13 경제성 EC-*)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` §12.7 EC-CACHE-01·03·04 · EC-BATCH-01·02 · EC-ROUTE-01 · EC-MODEL-01·02·07 · EC-CTRL-01·02·03·06, §20.1 M2 종료 게이트 "비용 레버 효과 측정"
- 범위 결정 (2026-10-02, 사용자): M2 1차 = Epic 13~15
- 공급자: mock (공급사 prompt cache 는 mock 이 Anthropic 형 읽기·쓰기로 흉내), 실 공급사 호출 없음

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E13-01 | 모델 카탈로그 (MODEL-07) | `GET /admin/econ/models` | 능력·지역·가격(캐시 단가 포함)·폐기 60일 전 경고, 과업 정책 ≥5 | P0 |
| E13-02 | 규칙 라우팅 (ROUTE-01) | `chat-econ` 짧은 요청 · pin · 긴 요청 | 짧고 도구 없음 → chat-small (`x-atlas-econ-route`), pin·긴 요청은 유지 | P0 |
| E13-03 | 과업 유형별 모델 정책·max_tokens (MODEL-01 · CTRL-02) | `x-atlas-task-type: classify` + chat-large · `model: auto` | 등급 상한으로 chat-small 하향, max_tokens 500→64 클램프, auto+reasoning → reasoner | P0 |
| E13-04 | reasoning effort 정규화 (MODEL-02) | `atlas.effort=high` + task code · 비추론 모델 | high→medium(과업 상한), thinking 예산 4096 → reasoning_tokens, 비추론 모델은 무시, max_tokens 기본값 주입 | P0 |
| E13-05 | exact 응답 캐시 (CACHE-04) | temperature 0 같은 요청 10회 · no-cache · temperature 0.7 | 1 MISS + 9 HIT-EXACT(비용 0), BYPASS, SKIP-TEMPERATURE, 테넌트 분리 | P0 |
| E13-06 | 공급사 prompt cache·KPI (CACHE-01·03) | 긴 고정 system + 질문 5회 | 캐시 키·TTL 지시, 2회차부터 cached_tokens·절감액, alias·테넌트 KPI hit_ratio>0.5·손해 캐시 경고 없음 | P0 |
| E13-07 | 계층 예산 (CTRL-01) | key 예산(downgrade) · org 예산(block) | 초과 시 chat-small 하향 → 429 budget_exceeded, 경고 헤더, 상태 exceeded, 기간 중 생성 예산도 기존 지출 반영 | P0 |
| E13-08 | 과업 토큰 예산 (CTRL-03) | `x-atlas-task-id` + budget 300 | 누적·헤더 `x-atlas-task-tokens`, 소진 시 429 task_budget_exceeded (AL-RUN 은 BUDGET_TOKENS 로 종료 — pytest) | P0 |
| E13-09 | 배치 (BATCH-01·02) | JSONL 3행(1행 실패) | in_progress → ended, 2 성공·1 실패, custom_id 보존 출력, 실패 행 재제출 round 1, 50% 할인, 미인증 401 | P0 |
| E13-10 | 비용 시뮬레이터 (CTRL-06) | `POST /v1/econ/simulate` | 워터폴 baseline→cache→batch→routing→model 단조 감소, 민감도 | P0 |
| E13-11 | 이벤트 | NATS GW 스트림 subject | gw.budget.state_changed · gw.budget.task_near_limit · gw.batch.completed | P0 |
| E13-12 | 콘솔 05 경제성 | UI E2E | 시뮬레이터 계산 → 워터폴 5행 | P1 |
| E13-13 | 회귀·prod | go/pytest/vitest · E3·E6 스모크 · prod 리허설 | 전부 PASS, gateway read_only·비루트 | P0 |
