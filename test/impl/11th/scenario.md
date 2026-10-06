# 시험 시나리오 — impl 11th · Epic 11 콘솔 v1

근거: PRD FR-PLT-CON-01(웹 콘솔 SPA·Manager embed.FS·도식 정보 구조)·CON-04(조직·프로젝트·환경 스위처·prod 확인), 화면설계서 SCR-B03(카드 = 메뉴 입구).

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E11-01 | 단위 시험 | `make test-all` | manager(svc 프록시·SPA fallback), console vitest(라우터·워터폴·svc URL), agent(런·실행 목록) | P0 |
| E11-02 | 콘솔 내장 | prod `GET :8080/` · 딥링크 · `/assets/*` | 번들 HTML, 딥링크 SPA fallback, 해시 자산 immutable 캐시, API 404 는 JSON | P0 |
| E11-03 | 서비스 프록시 | `/svc/{ob,alrun,ev,gr,mcpreg,gw}/…` | 200, 허용 목록 밖 404 | P0 |
| E11-04 | 섹션 렌더 | 7개 섹션 내비 | 각 화면 제목 렌더, 스크린샷 | P0 |
| E11-05 | 카드 → 화면 | 개요 맵 07 카드 클릭 | /guard | P1 |
| E11-06 | 가드 즉석 검사 | 검사 버튼 | transform 판정 | P1 |
| E11-07 | 런 실행 | 목표 입력 → 실행 | 런 상세 URL·이벤트 타임라인 | P0 |
| E11-08 | HITL | 비가역 목표 → 승인 버튼 | END_TURN | P0 |
| E11-09 | trace 워터폴 | DSL 검색 → 선택 | 워터폴 행 ≥ 2 | P0 |
| E11-10 | prod 전환 | 환경 prod | 확인 모달 → 색 띠·URL /e/prod | P0 |
| E11-11 | prod 변경 확인 | prod 에서 실행 | 확인 모달 | P0 |
| E11-12 | 런타임 오류 | 전체 흐름 | pageerror 0 | P0 |
| E11-13 | prod 리허설 | `make prod-all` → E11-02~12 를 :8080(내장 콘솔)으로 | 동일 | P0 |
| E11-14 | 회귀 | `make test-all` | exit 0 | P0 |
