# 시험 결과 / Test Result — impl 22nd (Epic 22 지식·메모리)

- 일자 Date: 2026-10-02
- 대상 Target: Gateway `POST /v1/embeddings`(mock 해싱 벡터·OpenAI 호환)·`DELETE /admin/econ/cache/exact`, KB 단위(`units/kb` — 커넥터·파이프라인·하이브리드 검색·메모리·삭제권·RAG 평가·작업 큐, 마이그레이션 0011 pgvector 분기), OB `POST /v1/erasure`, Manager 카탈로그 교차 단위(X-KB·X-SBX·X-MKT·X-RPT)·이벤트 중계 확장, compose `agent-kb`(8018)·`fake-upstream`, prod DB 이미지 `pgvector/pgvector:pg17`
- 환경 Env: Docker 29.8.0 · macOS Darwin 25.6.0 · Go 1.26 · Python 3.12 · PostgreSQL 17 + pgvector

## 요약 / Summary

- 전체 Total: 12, 통과 Pass: 12, 실패 Fail: 0, 최종 Verdict: **PASS** (local round 1 · prod round 1)
- 단위: pytest 158 passed(+7) · go test gateway ok(+1)

## 케이스 / Cases

| id | 결과 | 비고 |
|----|------|------|
| E22-01 | PASS | 256차원 결정적 임베딩 |
| E22-02 | PASS | 증분 3/3 · duplicate |
| E22-03 | PASS | SSRF 422 · HTML 정제 |
| E22-04 | PASS | ACL 403·숨김·노출 · 재순위 |
| E22-05 | PASS | prod vector(256)·HNSW·GIN |
| E22-06 | PASS | ADD/UPDATE/DELETE · 격리 2 |
| E22-07 | PASS | 병합 |
| E22-08 | PASS | 삭제권 4저장소 · 증명서 |
| E22-09 | PASS | EV 평가셋·RAG judge |
| E22-10 | PASS | 비동기 작업 |
| E22-11 | PASS | kb.search skill |
| E22-12 | PASS | plt.kb.* 이벤트 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 단위 | 일반 채팅 alias 로 임베딩 성공 | mock 공급자는 모든 모델에서 Embed 응답 | `mock-embed*` 만 허용, 4xx 는 400 으로 전달 | PASS |
| 단위 | heading 전략이 절을 합침 | 문단 병합 로직 공유 | heading 은 절 = 청크(긴 절만 고정 분할) | PASS |
| 설계 | KB 매니페스트 등록 불가 | Manager 카탈로그는 도식 47종만 | 교차 단위(X-KB·X-SBX·X-MKT·X-RPT) 별도 목록 — 47종 지도·시험은 그대로 | PASS |

## 설계 메모

- 벡터는 JSON(이식) + PostgreSQL 에서는 `embedding_v vector(256)`(HNSW cosine)·`tsv`(생성 열, GIN) — 검색은 `<=>` 와 `to_tsquery('simple', …)` 를 RRF(k=60)로 결합. SQLite 는 같은 RRF 를 파이썬 코사인·BM25 로.
- ACL 은 검색 전 필터(`acl_tags ∩ {user:…, group:…, public}`), `acl_enforce` 기본 true 면 신원 없는 검색 403.
- 메모리 쓰기: 출처 없음·신뢰도 < 0.5·GR-IO 인젝션 판정 → quarantine(불러오기 제외, release/discard). 통합은 코사인 ≥ 0.85 UPDATE, `잊어:`/`forget:` DELETE.
- 삭제권: KB 문서(주체 색인 `subjects`)·메모리·Gateway exact 캐시(주체 색인이 없어 전체 비움)·trace span 속성('[erased]' mutation) 순서, 증명서는 주체 원문 대신 sha256.

## 가정 / 후속

- 파서는 Markdown·HTML·텍스트(Docling·MinerU 외부 파서 컨테이너는 후속). Confluence·Drive·Notion·S3·Git 커넥터는 인터페이스(`Source.list/fetch`)만 — 원천 2종(코퍼스·URL) 구현.
- 작업 큐는 DB 테이블(Redis Streams·Temporal 대신). 메모리 병합은 결정적 문장 합집합(강한 모델 요약은 후속). 그래프 관계(Graphiti)는 후속.

## 첨부 / Logs

`logs/` (비커밋): local-all · E22-local · E22-prod · prod-build
