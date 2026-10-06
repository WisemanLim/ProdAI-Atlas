# 시험 시나리오 / Test Scenario — impl 17th (Epic 17 Multi-agent)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` MA-ORCH-01·02·03·04·05·07·09·10 · MA-RES-01·02 · MA-CR-01·02 · MA-ANL-01·03·05 · MA-MERGE-01·03 · A2A-SRV-05
- 범위 결정 (2026-10-02, 사용자): M2 2차 Epic 16~18 · 조사/리뷰 소스 = 내장 문서 코퍼스 + 로컬 diff · 샌드박스 = 전용 compose 서비스
- 실행: SDK `A2AClient` 로 Manager 경유 실제 위임. 오케스트레이터(ma-orch 8010) → 조사(ma-res)·리뷰(ma-cr)·분석(ma-anl → sandbox) → 병합(ma-merge). local(auth off) · prod(oidc, OpenBao 비밀 중개)

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E17-01 | 복잡 과업 병렬 위임·병합 (ORCH-01·03·04, MERGE-03) | 조사+리뷰+분석 목표, diff·CSV | multi·complex, 관점 3종 모두 COMPLETED, synthesis 병합, 보고서에 출처·이슈·수치(sales.mean) | P0 |
| E17-02 | 위임 명세·비용 지표·쓰기 단일 (ORCH-02·07·09) | E17-01 결과 | 명세 필드 완전, 서브 tools_allow=[], writes=orchestrator-only, 조정 토큰 지표 | P0 |
| E17-03 | 규모 결정 | 단순 질문 | simple·single·서브 1개 | P0 |
| E17-04 | fan-in first | join=first | 하나 완료 후 나머지 취소, 부분 결과 병합 | P0 |
| E17-05 | 마감 배리어 | 느린 분석 코드, deadline 4s | partial, 분석 FAILED/CANCELED, gaps 에 task_id | P0 |
| E17-06 | 서브 읽기 전용 강제 | tools_allow=[repo.write_file] | FAILED "읽기 전용" | P0 |
| E17-07 | 중첩 깊이 (ORCH-10) | depth=1 오케스트레이터 | FAILED max_depth | P0 |
| E17-08 | 조사 출처 필수 (RES-02) | 코퍼스 질의 | findings 마다 source_ids ⊆ sources, read_only | P0 |
| E17-09 | 코드 리뷰 구조화 + SARIF (CR-01·02) | 비밀 하드코딩·SQL 연결 diff | rule 2종, SARIF 2.1.0, critical | P0 |
| E17-10 | 분석 샌드박스 (ANL-01·03·05) | CSV · 네트워크 코드 · 1.5GB 할당 · 무한 루프 | vega-lite bar, 표 3행, 네트워크·메모리 실패, 시간 한도 FAILED | P0 |
| E17-11 | 구조적 병합 (MERGE-01) | 중복 이슈 | structural, 중복 제거, 최고 심각도 | P0 |
| E17-12 | skill 보안 요구 (SRV-05) | 오케스트레이터 직접 무토큰 | -32051 | P0 |
| 단위 | pytest test_ma.py · go test | — | 전부 PASS | P0 |
