# 시험 시나리오 / Test Scenario — impl 21st (Epic 21 성숙도 MAT + 콘솔 M3 · M3 1차 게이트)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` MAT-01·02·03·04·05·06·08·09·10 · 20.1 M3 종료 게이트("MAT L3 달성 · 모델 교체 자동화")
- 범위 결정 (2026-10-02, 사용자): M3 1차 Epic 19~21
- 실행: `e21_mat.py` 는 실행별 프로젝트(`e21-<nonce>`)로 스냅샷 이력을 분리하고, 게이트가 요구하는 운영 사실을 실제 서비스 API 로 만든 뒤(가상키·예산·계산 정책·prompt cache·골든셋 50·회귀 10회·judge 보정·온라인 규칙·trace 승격·SLO·경보·가드 판정·승인 결정·모델 후보·프롬프트 게시) MAT 를 다시 진단한다. 끝나면 예산·정책·후보·alias 를 정리한다. `e21_console.py` 는 콘솔 M3 화면 E2E.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E21-01 | 4속성 점수 (MAT-01) | 실제 텔레메트리 | 4속성 모두 ≥ 70, 출처 오류 없음, 판정 = pass·fail·unknown | P0 |
| E21-02 | L0~L4 단계·연속 게이트 (MAT-02) | — | L4 Autonomous Multi-agent · Compound · 4단계 게이트 모두 통과 | P0 |
| E21-03 | 9개념(+A2A) 커버리지 (MAT-03) | Manager 레지스트리 | 10개념, 8개 이상 도입, 루프·다중 에이전트 ● | P1 |
| E21-04 | 복리 루프 (MAT-04) | trace 승격·후보 평가 | 승격 ≥1, 모델 교체 회귀 0, loop_running | P0 |
| E21-05 | 90일 로드맵 (MAT-05) | 시작 vs 끝 | 끝: 카드 0, 시작 카드 역할 4종 안 | P1 |
| E21-06 | 공통 지표 (MAT-06) | — | 7종, cache_hit ≥ 0.2, 값 존재 | P0 |
| E21-07 | 스냅샷·리포트 (MAT-08) | 시작·끝 스냅샷 | 이력 2, L4, 리포트·조직 롤업 | P1 |
| E21-08 | 홈 진화 띠 (MAT-09) | Manager `/svc/mat` (콘솔 경로) | stage 4 · Compound · 100% · 미달 없음 · 4속성 | P0 |
| E21-09 | 성과 지표 (MAT-10) | 기준선 = 첫 스냅샷 | 3카드 · 기준선 시각 일치 | P1 |
| E21-10 | 단계 변경 이벤트 | 기준선과 단계가 다르면 | `plt.mat.stage_changed` PLT 스트림 | P1 |
| E21-11 | A2A skill | `mat.assess` | COMPLETED · stage 4 | P1 |
| E21-12 | 콘솔 M3 E2E | 홈 띠 → 성숙도 · Bitter Lesson · 평가 | 마커·커버리지 10행·플래그·judge·스코어카드·오류 없음 | P0 |
| 단위 | pytest test_mat(4) · vitest(maturity helper) · go test(감사 검증 에이전트 허용) | — | PASS | P0 |
| GATE | `m3a-gate.sh` | test-all · 레드팀 · prod Epic 1~21 · UI v1·M2·M3 · 하드닝 | 전부 PASS | P0 |
