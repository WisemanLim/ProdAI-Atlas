# 시험 시나리오 — impl 10th · Epic 10 Evals (+ M1 2차 완료 게이트)

근거: PRD FR-EV-GOLD-01·02·03·06 · EV-REG-01·02·03·04·06, §1.3-5 신뢰성 폐루프(trace → 골든셋 승격 → 회귀 → 게이트).

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E10-01 | 단위 시험 | `make test-all` | agent pytest(EV 10 포함) | P0 |
| E10-02 | 골든셋 시드 | `make eval-seed` | 3 항목 bulk → in_review → approved(큐레이터) → v1 스냅샷 | P0 |
| E10-03 | 회귀 게이트 통과 | `make eval` (prompt·agent 타깃 × epochs 3) | exit 0, gate PASS, pass^k 1.0 | P0 |
| E10-04 | 게이트 실패 | 불가능한 채점기 스위트 | exit 100 + 실패 사유 | P0 |
| E10-05 | 실행 오류 | 없는 데이터셋 | exit 2 | P0 |
| E10-06 | trace → 골든셋 | 전화번호 포함 요청 trace → items:from-trace | in_review, PII 마스킹, source_trace_id | P0 |
| E10-07 | 검수 | 비큐레이터 승인 → 큐레이터 승인 | 403 → approved, 라벨 이력 | P0 |
| E10-08 | 불변 버전 | 승인 항목 수정 · 새 스냅샷 | 409 · v2 에 승격 항목, v1 그대로 | P0 |
| E10-09 | 비용 귀속 | eval 실행 후 `/v1/costs?group_by=agent` | EV-REG 비용 집계 | P1 |
| E10-10 | prod 리허설 | `make prod-all` → E10-02~09 | 동일 | P0 |
| E10-11 | 회귀·M1 2차 게이트 | `m1b-gate.sh` | Epic 1~10 스모크 전부 PASS | P0 |
