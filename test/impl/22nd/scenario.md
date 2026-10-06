# 시험 시나리오 / Test Scenario — impl 22nd (Epic 22 지식·메모리)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` X-KB-01~09
- 범위 결정 (2026-10-02, 사용자): M3 2차 Epic 22~24 · 벡터 = pgvector(prod) + SQLite 브루트포스(local) · 임베딩 = Gateway mock(결정적 해시) + OpenAI 호환 선택
- 실행: `e22_kb.py` 가 실제 Gateway `/v1/embeddings`·KB(`agent-kb` 8018)·GR-IO·EV·OB(ClickHouse)·가짜 업스트림(`fake-upstream`)·NATS 와 (prod) PostgreSQL pgvector 를 호출.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E22-01 | Gateway 임베딩 | 3문장 | 256차원·L2=1·결정적·어휘 유사도·비용 헤더 | P0 |
| E22-02 | 코퍼스 증분 동기화·중복 제거 (KB-01·02) | 번들 정책 문서 3 | 1회 추가 3 · 2회 불변 3 · 같은 내용 업로드 duplicate | P0 |
| E22-03 | URL 원천·HTML 정제·SSRF | 가짜 업스트림 문서 2 · 메타데이터 주소 | 422 거부 · 추가 2 · 태그·스크립트 제거 | P0 |
| E22-04 | 하이브리드 검색·ACL·재순위 (KB-03·04) | 신원 없음·일반·재무 그룹 · 콘솔 경로 | 403 · 재무 문서 숨김 · 재무 그룹은 봄 · 출처·점수·ACL 필드 | P0 |
| E22-05 | 저장 엔진 | prod 스키마 | `embedding_v vector(256)` · HNSW·GIN 인덱스 · 벡터 전부 채움 | P0 |
| E22-06 | 메모리 4종·쓰기 검증 (KB-05·06) | 의미·에피소드 · 출처 없음 · 인젝션 | ADD·UPDATE(같은 항목)·DELETE · 격리 2건 불러오기 제외 | P0 |
| E22-07 | 비동기 정리 (KB-07) | 유사 절차 2 | 병합 1 · 내용 합집합 | P1 |
| E22-08 | 삭제권 전파 (KB-08) | 주체 문서·메모리·trace·exact 캐시 | 증명서 complete · 각 저장소 건수 · trace 원문에 주체 없음 · 캐시 MISS | P1 |
| E22-09 | RAG 평가 연동 (KB-09) | 질문 2 | EV 평가셋 2항목 · RAG judge · recall 1.0 | P1 |
| E22-10 | 비동기 인덱싱 큐 | wait=false | 작업 done · indexed | P1 |
| E22-11 | A2A skill kb.search | Manager agent-proxy | COMPLETED · 출처 shipping.md | P0 |
| E22-12 | 이벤트 | NATS PLT | plt.kb.erasure.completed · plt.kb.memory.consolidated | P1 |
| 단위 | pytest test_kb(7) · go test(임베딩) | — | PASS | P0 |
