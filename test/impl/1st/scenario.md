# 시험 시나리오 — impl 1st · Epic 1 Manager 카탈로그

근거: PRD FR-PLT-MGR-01·03·04, §3.3 카탈로그, §14.4 이벤트(`plt.agent.*`), §17.2 Manager API, COMPLIANCE CMP-02.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E1-01 | 단위 시험 | `make test APP_SVC=manager` | go test ok (catalog·manifest·api·OpenAPI 정합) | P0 |
| E1-02 | local 마이그레이션 | `make db-migrate` | exit 0, SQLite 스키마 적용 | P0 |
| E1-03 | 카탈로그 | `GET /api/v1/catalog/units` | 47개 | P0 |
| E1-04 | 등록 | `POST /api/v1/agents` (AL-PLAN 매니페스트) | 201, `layer=L1`, `currentRev=1` | P0 |
| E1-05 | 검증 오류 | `POST` unitId=NOPE·image 태그 | 422 `invalid_manifest` + details | P0 |
| E1-06 | 이름 중복 | 같은 매니페스트 재등록 | 409 | P0 |
| E1-07 | 리비전 no-op | 같은 spec PATCH | `changed=false`, rev 1 | P0 |
| E1-08 | 리비전 생성 | version 변경 PATCH | `changed=true`, rev 2, versions 2건 | P0 |
| E1-09 | 소프트 삭제 | `DELETE` → `GET` | 204 → 404, 이름 재등록 가능 | P0 |
| E1-10 | 이벤트 발행 | NATS `/jsz?streams=true` | 스트림 `PLT` 메시지 수 = 발행 수 | P0 |
| E1-11 | OpenAPI | `GET /api/v1/openapi.json` | 200, openapi 3.1.0 | P1 |
| E1-12 | prod(PostgreSQL) | `make prod-all` → `make db-migrate ENV=prod` → E1-04·07·08·09 반복 | 동일 결과 (부분 유니크 인덱스 포함) | P0 |
| E1-13 | 회귀 | `make test-all` | exit 0 | P0 |
