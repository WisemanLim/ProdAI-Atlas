# 시험 시나리오 / Test Scenario — impl 19th (Epic 19 Judge·점수)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` EV-JUDGE-01·02·03·04·05·06·08·09 · EV-SCORE-01·02·03·04·05·07·08·09
- 범위 결정 (2026-10-02, 사용자): M3 1차 Epic 19~21 · judge = Gateway mock judge(결정적) + OpenAI 호환 선택
- 실행: `e19_judge.py` 가 실제 Gateway(`judge-*` alias → mock-judge)·EV·OB(ClickHouse)·GR 을 호출. 온라인 채점은 새 가상키 테넌트로 만든 실제 trace 를 `atlas.tenant` DSL 로 고른다.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E19-01 | 루브릭 judge 버전·binary+critique (JUDGE-01·02) | 같은 이름 2회 생성 · 합/불 출력 | v1·v2 불변 버전, pass/fail + critique | P0 |
| E19-02 | pairwise 위치 교차 (JUDGE-03) | 일반·편향(항상 A) judge | B·일관 / tie·불일치 | P1 |
| E19-03 | 배심원 (JUDGE-04) | strict·lenient·length 3 alias | 다수결 pass, 일치도 0.6667 | P1 |
| E19-04 | 사람 일치도 보정 (JUDGE-05) | 사람 라벨 10건 | κ=1 trusted · 길이 judge κ<0.6 untrusted | P0 |
| E19-05 | judge 채점 스위트·게이트 | 프롬프트 v1·v2 × 승인 5항목 | v1 fail 0.0 · v2 pass 1.0 · 미보정 judge 게이트 차단 | P0 |
| E19-06 | 실험 diff·유의성 (SCORE-02·03) | v1→v2 · v2→v1 | fail→pass 5, McNemar p 0.0625, CI [1,1], 역방향 regressed·퇴행 우선 | P0 |
| E19-07 | criteria drift shadow (JUDGE-06) | v1 점수 → v2·v3 재채점 | 표본 10, v2 변경 0 · v3 pass→fail 5 (0.5) | P1 |
| E19-08 | RAG judge (JUDGE-08) | 주장 2개 중 1개 근거 | faithfulness 0.5 · precision 0.5 · recall 0.5 | P1 |
| E19-09 | 온라인 샘플링 → 골든셋 후보 (JUDGE-09 · SCORE-07) | 실제 trace 3 (PII 포함) | 3 채점 · 2 저점 → in_review 후보 2(마스킹) · 재실행 중복 0 | P0 |
| E19-10 | 사용자 피드백 (SCORE-08) | 👎 | 점수 기록 + 후보 · trace 점수 online+feedback | P1 |
| E19-11 | 추세·드리프트·스코어카드 (SCORE-04·05·09) | — | 실행 순 통과율, 드리프트 응답, 타깃별 성공률·과업당 비용 | P0 |
| E19-12 | judge 비용 귀속 | OB 비용 group_by agent | EV-JUDGE > 0 | P1 |
| 단위 | pytest test_ev_judge(4) · go test provider(mock judge) | — | PASS | P0 |
