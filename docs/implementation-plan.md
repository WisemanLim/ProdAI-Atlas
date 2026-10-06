# 구현 계획표 / Implementation plan — M0 Foundation

> wds-implement 가 갱신한다. 근거: `.docs/ProdAI-Atlas_PRD.pdf` v1.0 §20.1 M0 · §5.5 · §11.13~11.15 · §12 · §17.
> 범위 결정 (2026-10-02, 사용자 확인): **M0 Foundation** 만 구현 · Gateway 공급자 = mock 기본 + OpenAI 호환 upstream env · Gateway = Go 별도 서비스 `apps/gateway`.
> M0 종료 게이트(PRD §20.1): "에이전트 1개 독립 실행·배포 E2E" — **충족 (2026-10-02, `test/impl/5th/m0-gate.sh` 36 PASS)**.
> 최종: DONE 5 · BLOCKED 0.

| # | Epic | 기능(FR) | 수용 기준 / 시험 케이스 | 의존 | 차수 | 상태 |
|---|------|----------|-------------------------|------|------|------|
| 1 | Manager 카탈로그 | PLT-MGR-01·03·04 (등록·리비전·소프트 삭제), 단위 카탈로그 47종, goose 마이그레이션(SQLite/PG), NATS `plt.agent.*` (CloudEvents) | CRUD·검증 오류·리비전 no-op·삭제, 이벤트 수신, OpenAPI 라우트 정합(CMP-02), local/prod 마이그레이션 | — | 1st | DONE |
| 2 | Atlas Agent Spec + Python SDK | A2A-SRV-01·02·04·06 (어댑터·SendMessage/GetTask/ListTasks/CancelTask·tenant·Task 상태머신/영속), A2A-CARD-01, PLT-PKG 매니페스트, `/healthz`·`/readyz`, 샘플 AL-PLAN | Card 필수 필드, JSON-RPC 오류 코드, 상태 전이·tenant 격리, 단독 기동 ≤ 5s(NFR-PERF-03), Manager 자가 등록 | 1 | 2nd | DONE |
| 3 | AI Gateway (GW-* 6종) | ROUTE-01·03·05·07·08, AUTH-01·05, RATE-01·02, KEY-01·06, COST-01·03·04, POL-01·04·05 | mock E2E(비스트림/SSE), 가상키 401/403, 429+Retry-After, 폴백, 비용 헤더·`gw.request.completed`, 감사 해시 체인, 추가 지연 p95 측정 | 1 | 3rd | DONE |
| 4 | OB-TRACE | OB-TRACE-01·04·05·06 (OTLP 수신 Collector · 컨텍스트 전파 · SDK 계측 · 게이트웨이 span 결합) | 같은 trace_id 로 agent·gateway span 수집, gen_ai.* 속성 | 2·3 | 4th | DONE |
| 5 | 배포 E2E + 콘솔 v0 | PLT-DEP(배포 레코드·상태·`plt.deploy.*`), PLT-MGR-02(Card 수집)·05(헬스 폴링)·10(agent-proxy), PLT-CON 개요 맵 v0 | 등록→배포→ready→Manager 경유 A2A 호출→Gateway→trace E2E, 콘솔 렌더 | 1~4 | 5th | DONE |

## M1 (1차) — 2026-10-02 사용자 확인: Epic 6~8 · 가드레일은 규칙·정규식 + 한국형 PII 인식기

| # | Epic | 기능(FR, P0) | 수용 기준 / 시험 케이스 | 의존 | 차수 | 상태 |
|---|------|--------------|-------------------------|------|------|------|
| 6 | Agentic Loop 런타임 | AL-PLAN-05 · AL-ACT-01·02·03·05 · AL-OBS-01·02·03 · AL-REF-01·02 · AL-EXIT-01·02·03·05·06·07·08 · AL-RUN-01·02·03·05·08 (+ GW tool calling 통과) | 루프 상태기계, 종료 사유 12종 enum·RunResult, 스키마 검증·병렬·멱등, HITL 정지·재개, 반복 탐지, 체크포인트·재개, 압축, 이벤트 스트림 | M0 | 6th | DONE |
| 7 | MCP 핵심 | MCP-HOST-01·02·06·07·09 · MCP-REG-01·03·05·06 · MCP-SEL-01·02·03 · MCP-TOOL-01·03 | 레지스트리 API v0.1·허용목록·도구 고정(rug pull 차단)·설치 전 스캔, 클라이언트 풀·버전 기록·승인 규칙·토큰 패스스루 금지·감사 span, BM25 top-k·search_tools, OpenAPI→MCP | 6 | 7th | DONE |
| 8 | Guardrails | GR-IO-01·02·04·06·08 · GR-POL-01·02·04·05·07 · GR-JB-01·03·07 · GR-PII-01·02·05·07 (+ GW-POL-02 훅, AL-EXIT-07) | 경계별 파이프라인·cascade, 한국형 PII(체크섬)·마스킹·일관 가명, 탈옥 규칙 분류·spotlighting·레드팀 ASR 게이트, PDP·도구 인가·비가역 HITL·기본 정책 팩, Gateway 차단/치환, 로그·trace 정화 | 6·7 | 8th | DONE |

**M1 1차 결과 (2026-10-02)**: DONE 3 · BLOCKED 0 · 완료 게이트 `test/impl/8th/m1-gate.sh` 62 PASS.

M1 1차 가정: MCP OAuth 2.1(HOST-03)·게이트웨이 모드(HOST-04)·양방향 중계(HOST-05)·서명 검증(REG-04)·임베딩 하이브리드 검색·OPA/Rego·Colang·Presidio NER·분류 모델(Prompt Guard)·워커 lease 자동 인수는 M2 이후. 출력 스트리밍 청크 검사(GR-IO-03)는 Gateway 가 업스트림 스트림을 중계하지 않는 M0 구조라 전체 응답 검사로 대체.

## M1 (2차) — 2026-10-02 사용자 확인: Epic 9~10 · trace 저장소 ClickHouse 컨테이너

| # | Epic | 기능(FR, P0) | 수용 기준 / 시험 케이스 | 의존 | 차수 | 상태 |
|---|------|--------------|-------------------------|------|------|------|
| 9 | Observability | OB-TRACE-03·07 · OB-LOG-01·02·03·06 · OB-DEBUG-01 · OB-DASH-01·03·04·05 | ClickHouse 저장(30일 TTL), 검색 DSL·span 트리·세션, 본문 기록 정책·비밀 누출 차단·결정 이벤트, trace 재실행 비교, 대시보드·SLO 번레이트·경보·비용 귀속 | M0·6~8 | 9th | DONE |
| 10 | Evals | EV-GOLD-01·02·03·06 · EV-REG-01·02·03·04·06 | 골든셋 CRUD·검수 상태머신·불변 버전·trace 승격, eval-as-code·타깃 3종·코드 채점기·pass^k·CI 게이트 exit 0/100/2 | 9 | 10th | DONE |

**M1 2차 결과 (2026-10-02)**: DONE 2 · BLOCKED 0 · 완료 게이트 `test/impl/10th/m1b-gate.sh` 79 PASS.

M1 2차 가정: trace 요약 1년 롤업·tail sampling·테넌트 헤더 매핑·TTFT·캐시 적중률, OTel Logs(결정 로그는 span 이벤트로 대체), promptfoo import·샌드박스 채점기·EV-JUDGE·S3/Parquet·배포 게이트 연동은 후속. 남은 M1: 콘솔 v1(11)·패키징·릴리스(12).

## M1 (3차) — 2026-10-02 사용자 확인: Epic 11~12 · 데스크톱(Tauri 2) macOS 빌드 포함

| # | Epic | 기능(FR, P0) | 수용 기준 / 시험 케이스 | 의존 | 차수 | 상태 |
|---|------|--------------|-------------------------|------|------|------|
| 11 | 콘솔 v1 | PLT-CON-01·04 + 화면(루프·MCP·가드·Trace·Evals) | Manager 가 콘솔을 embed.FS 로 서빙 + `/svc/*` 플랫폼 서비스 프록시, 조직·프로젝트·환경 스위처(URL 반영·prod 색 띠·확인 모달), 섹션 7개 렌더·동작(런 시작·HITL 승인·trace 워터폴·검수 큐) | 6~10 | 11th | DONE |
| 12 | 패키징·릴리스 | PLT-PKG-01·02·03·07·08 · PLT-REL-01·02·03·04·05·08 + 데스크톱(§11.17) | 멀티아치 이미지·비루트·OCI 라벨, wheel, 단일 바이너리 6타깃, compose 번들·install.sh, `atlas install`, SemVer·compat.json·채널, release-please·GoReleaser·cosign·SBOM·라이선스 CI, 업그레이드 백업·dry-run, Tauri macOS .app | 11 | 12th | DONE |

**M1 3차 결과 (2026-10-02)**: DONE 2 · BLOCKED 0 · M1 최종 게이트 `test/impl/12th/m1-final-gate.sh` 108 PASS. **M1 전체 완료 (Epic 6~12 DONE 7 · BLOCKED 0).**

M1 3차 가정: 실제 GHCR·PyPI·GitHub Release 게시와 cosign keyless(Rekor)는 첫 태그의 CI 몫 — 로컬은 `registry:2` + 리허설 키쌍으로 서명·검증·승격을 검증. Helm/Operator·`atlas install` kubernetes 모드는 M2. 데스크톱은 macOS .app(ad-hoc 서명)만 실빌드, Windows·Linux·universal·공증은 `release.yml` tauri-action 정의만. 콘솔 IAM(로그인·RBAC)은 M2(PLT-IAM) — 콘솔 조직/프로젝트/환경은 URL 컨텍스트만.

## COMPLIANCE testing_additions 배분

| 항목 | 배분 |
|------|------|
| OpenAPI 계약 적합성 (CMP-02) | Epic 1 (Manager), Epic 3 (Gateway 관리 API) |
| 크로스테넌트 누출 차단 (CMP-01) | Epic 2 에서 A2A Task tenant 파티션만 · RLS 는 M2(PLT-TEN) |
| 감사로그 보관 경과 삭제 (CMP-03) | M2 (Epic 3 은 해시 체인 기록까지) |

## 가정 / Assumptions

- PRD.md 가 없어 PDF 를 정본으로 사용. 요구 변경 없음 → PRD 갱신 없음.
- A2A: a2a-sdk 래핑 대신 와이어 호환 최소 구현(JSON-RPC 바인딩만). a2a-sdk 래핑·gRPC·HTTP+JSON·스트리밍·Push 는 M1.
- 비밀은 env 주입(OpenBao 는 M2). IAM·RBAC(casbin)·SSO 는 M2 → Gateway 관리 API 는 관리 토큰으로 보호.
- 서명(JWS Agent Card·cosign)·Helm/Operator·GoReleaser·Tauri 패키징은 M1 이후.
- trace 저장소 ClickHouse 는 M1 → M0 는 OTel Collector file exporter 로 수집 검증.
- compose 드라이버: Manager 가 Docker 소켓을 쓰지 않고 HTTP `/readyz` 폴링으로 상태 수집(보안상 소켓 마운트 회피).

## M2 (1차) — 2026-10-02 사용자 확인: Epic 13~15 · IAM = Keycloak + OpenFGA 컨테이너 · 비밀 = OpenBao 컨테이너 + 파일 드라이버 · (Epic 17 시) 샌드박스 = 전용 compose 서비스

| # | Epic | 기능(FR, P0) | 수용 기준 / 시험 케이스 | 의존 | 차수 | 상태 |
|---|------|--------------|-------------------------|------|------|------|
| 13 | 경제성 (EC) | EC-CACHE-01·03·04 · EC-BATCH-01·02 · EC-ROUTE-01 · EC-MODEL-01·02·07 · EC-CTRL-01·02·03·06 | 공급사 prompt cache 마커·적중/절감 지표, exact 응답 캐시, Batch API(JSONL)·상태, 규칙 라우팅, 과업 유형별 모델 정책·reasoning effort·모델 카탈로그, 계층 예산(80% 경고·초과 차단)·max_tokens 정책·과업 토큰 예산, 비용 레버 효과 측정 | M1 | 13th | DONE |
| 14 | 기업·IAM·격리 | PLT-TEN-01·02·03·05·06 · PLT-IAM-01·04·06·08 (+ CMP-01) | 조직 수명·org/project/env 계층·쿼터, PostgreSQL RLS 크로스테넌트 누출 차단, A2A 테넌트 전파(JWT), Keycloak OIDC SSO(콘솔·API), OpenFGA RBAC 역할 템플릿, 서비스 계정(client credentials), 변경 불가 감사 로그(해시 체인) | 13 | 14th | DONE |
| 15 | 비밀 | PLT-SEC-01·03·04·08 | SecretStore 추상화(파일·OpenBao), 파일 주입 드라이버(env 비밀 기본 비활성), 공급자 키 이중 슬롯 무중단 회전, 에이전트별 최소 권한·비밀 read 감사 | 14 | 15th | DONE |

**M2 1차 결과 (2026-10-02)**: DONE 3 · BLOCKED 0 · 완료 게이트 `test/impl/15th/m2a-gate.sh` **160 PASS** (round 3). M2 종료 게이트 중 "멀티테넌트 격리 시험"(E14-04·16, RLS)·"비용 레버 효과 측정"(13th 레버 표: 라우팅 77.5% · 모델 정책 98.6% · exact 캐시 90% · prompt cache 66.4% · 배치 50%) 충족.

M2 1차 가정: local 기본 인증 off(개발), prod oidc. 비밀은 Manager 중개 모델(에이전트별 OpenBao 정책 대신), OpenBao unseal 키는 리허설용 볼륨 보관(운영 KMS auto-unseal = SEC-02 P1). 런타임(Python) 테이블 RLS·SAML·SCIM·ABAC·MFA·워크로드 신원·감사 외부 앵커는 후속. 기존 스모크(인증 이전 작성)는 시험 shim 이 게이트 API 키를 붙인다. impl 8th·9th·10th 스크립트는 M2 전제(조직=tenant·수집 지연)에 맞춰 갱신(주석 표시).

다음 차수 후보(M2 2차): 16 A2A 3종 · 17 다중 에이전트(샌드박스 compose 서비스) · 18 승인함·알림 + 콘솔 M2 화면.

## M2 (2차) — 2026-10-02 사용자 확인: Epic 16~18 · 알림 = 웹훅 + 이메일(Mailpit 컨테이너) · MA RES/CR 원천 = 내장 문서 코퍼스 + 입력 diff · ANL 샌드박스 = 전용 compose 서비스

| # | Epic | 기능(FR, P0) | 수용 기준 / 시험 케이스 | 의존 | 차수 | 상태 |
|---|------|--------------|-------------------------|------|------|------|
| 16 | A2A 3종 | A2A-CARD-01·02·03·06 · A2A-CLI-01·02·03·04·06·07 · A2A-SRV-03·05 | Manager 카드 게시·JWS(ES256, JCS) 서명·검증, skill 검색 레지스트리·테넌트 가시성, 클라이언트 카드 해석·원격 위임·호출자 인증·Task 추적(폴링·SSE)·INPUT_REQUIRED 중계·취소/타임아웃, Push 알림(웹훅 서명), 카드 securitySchemes 강제 | M2 1차 | 16th | DONE |
| 17 | 다중 에이전트 | MA-ORCH-01·02·03·04·05·07·09·10 · MA-MERGE-01·02·03·06·08 · MA-RES-01·02·03 · MA-CR-01·02·04 · MA-ANL-01·03·05 | 과업 분해·위임 명세·규모 결정·병렬 fan-out·fan-in 배리어(부분 결과)·조정 비용·쓰기 단일화·깊이/스폰 한도, 결과 정규화·병합 전략·LLM 합성·구조 병합·병합 후 검증, 조사(출처 필수·읽기 전용), 코드 리뷰(diff·구조화 이슈·읽기 전용), 분석(샌드박스 코드 실행·자원 한도·표/차트) | 16 | 17th | DONE |
| 18 | 승인함·알림 + 콘솔 M2 | X-HITL-01·02·07 · X-NTF-01·02·03 (+ 콘솔 경제성·IAM·비밀·승인함·A2A 화면) | 승인 요청 통합 수집(루프 HITL·도구·배포)·승인/거부 처리·감사, 이벤트 → 규칙 → 웹훅(서명)·이메일(Mailpit) 다채널, 예산·쿼터 알림, 콘솔 화면, M2 완료 게이트 | 16·17 | 18th | DONE |

M2 2차 결과 (2026-10-02): DONE 3 · BLOCKED 0 — 16th 12/12 · 17th 12/12 (local r4 · prod r3) · 18th 12/12 (local r5 · prod r2). **M2 완료 게이트 `test/impl/18th/m2-gate.sh` 205 PASS (round 2).**
M2 2차 가정: 승인 자동 등록은 루프 HITL(도구·배포는 API 직접), 조사·리뷰 원천은 내장 코퍼스·입력 diff, 알림 이메일은 Mailpit(운영은 SMTP 릴레이). A2A P1(외부 카드 수집·버전·위임 트리 콘솔·gRPC)·승인 정책(2인·위임)은 후속.

## M3 (1차) — 2026-10-02 사용자 확인: Epic 19~21 · judge = Gateway mock judge(결정적) + OpenAI 호환 선택 · 보정 플래그·하네스 = OpenFeature flagd 컨테이너

| # | Epic | 기능(FR) | 수용 기준 / 시험 케이스 | 의존 | 차수 | 상태 |
|---|------|----------|-------------------------|------|------|------|
| 19 | Judge·점수 | EV-JUDGE-01·02·03·04·05·06·08·09 · EV-SCORE-01·02·03·04·05·07·08·09 | 루브릭 judge 버전·binary+critique·pairwise 위치 교차·배심원 집계·사람 일치도(Cohen κ)·shadow 재채점 drift·RAG faithfulness·온라인 샘플링 채점, 점수 저장(ClickHouse)·실험 diff·부트스트랩 CI·McNemar·추세·드리프트(EWMA)·저점 trace → 골든셋 후보·피드백 API·스코어카드 | M1·M2 | 19th | DONE |
| 20 | Bitter Lesson | BL-MODEL-01~08 · BL-HARN-01~06 · BL-COMP-01·02·03·05·06 | 모델 카탈로그·별칭 버전·후보 등록 → 회귀 평가 → 채택 규칙 DSL → 카나리 가중 라우팅 → 별칭 전환(폴백 유지)·수명종료 경고·모델 카드, 프롬프트 레지스트리(버전·라벨)·규칙 외부화·harness.yaml·flagd 보정 플래그·ablation·게시 게이트, 계산 정책(effort)·난이도 배분·best-of-n 검증·추론 토큰 예산·품질-계산 곡선 | 19 | 20th | DONE |
| 21 | 성숙도 진단 | MAT-01·02·03·04·05·06·08·09·10 | 4속성 점수·L0~L4 단계·9개념 커버리지·복리 루프 지표·로드맵 카드·지표 레지스트리·월 스냅샷/리포트·홈 진화 띠·성과 지표, 콘솔 화면, M3 1차 게이트 | 19·20 | 21st | DONE |

M3 1차 제외(P2): EV-JUDGE-07·10, EV-SCORE-06, BL-MODEL-09, BL-HARN-07·08, BL-COMP-04·07, MAT-07·11.

M3 1차 결과 (2026-10-02): DONE 3 · BLOCKED 0 — 19th 12/12 (local r3 · prod r1) · 20th 12/12 (local r3 · prod r1) · 21st 12/12 (local r3 · prod r2). **M3 1차 게이트 `test/impl/21st/m3a-gate.sh` 248 PASS (round 2)** — PRD M3 종료 게이트: MAT L4 도달 · 모델 교체 자동화(후보 → 회귀 → 카나리 → 전환·롤백) 측정.
M3 1차 가정: 점수는 EV 관계형 저장소(ClickHouse 대신), LiteLLM 가격표 동기화·GrowthBook 미사용(내장 통계), CEL 대신 내장 식 평가기, MAT 리포트 Markdown. 다음 차수 후보(M3 2차): X-KB 지식·메모리 · X-SBX 확장 + MCP-SYS·MCP-RES · X-MKT·X-RPT.

## M3 (2차) — 2026-10-02 사용자 확인: Epic 22~24 · 벡터 = pgvector(prod) + SQLite 브루트포스(local) · 임베딩 = Gateway mock(결정적 해시) + OpenAI 호환 선택 · MCP-SYS = 로컬 가짜 업스트림 + PostgreSQL 읽기 전용 커넥터 실연동

| # | Epic | 기능(FR) | 수용 기준 / 시험 케이스 | 의존 | 차수 | 상태 |
|---|------|----------|-------------------------|------|------|------|
| 22 | 지식·메모리 | X-KB-01~09 | KB 생성·수집 커넥터·청킹/임베딩 파이프라인·하이브리드 검색(벡터+FTS, 재순위)·ACL 전파·에이전트 메모리(에피소드·의미·절차)·메모리 쓰기 검증(인젝션·출처)·비동기 정리·삭제권 전파·RAG 평가 연동 | M3 1차 | 22nd | DONE |
| 23 | 샌드박스·MCP 시스템·리소스 | X-SBX-01·02·03·06·07 · MCP-SYS-01~06 · MCP-RES-01~06 | 코드 실행 skill·자원/네트워크 상한·격리 수준 선택·실행 감사·저신뢰 MCP 격리, 표준 커넥터 팩·자격증명 브로커·stdio 샌드박스·egress/SSRF 차단·DB 읽기 전용 커넥터·헬스/SLA, 리소스 카탈로그·URI 템플릿·구독·캐시 TTL·접근 경계·민감 마스킹 | 22 | 23rd | DONE |
| 24 | 마켓·리포트 | X-MKT-01~06 · X-RPT-01~06·08 | 템플릿 카탈로그·원클릭 설치(파라미터 폼)·서명/출처 검증·사내 게시(승인)·업데이트 알림·MCP 서버 카탈로그 동기화, 사용량 계측·비용 그룹핑·차지백·감사 저장/조회·SIEM 내보내기·정기 경영 보고서·이상 사용 탐지, 콘솔 화면, M3 완료 게이트 | 22·23 | 24th | DONE |

M3 2차 제외(P2): X-SBX-05 · X-MKT-07 · X-RPT-07. X-SBX-04(브라우저·데스크톱 샌드박스)는 P1 이나 브라우저 런타임 이미지(GB 단위) 부담으로 후속(가정).

M3 2차 결과 (2026-10-02): DONE 3 · BLOCKED 0 — 22nd 12/12 (local r1 · prod r1) · 23rd 15/15 (local r4 · prod r1) · 24th 16/16 (local r5 · prod r2). **M3 완료 게이트 `test/impl/24th/m3-gate.sh` 301 PASS (round 3).**
M3 2차 가정: 격리 수준 container 만(gVisor·microVM 미가용 → 422), 커넥터 업스트림 = 가짜(`fake-upstream`), 템플릿 인덱스 = 메타 DB(OCI/ORAS 후속)·서명 = 내장 ed25519(cosign·Rekor 후속), WORM = 데이터 볼륨 봉인 세그먼트(S3 Object Lock 후속), Parquet·외부 과금 미지원, X-RPT 수집 = Manager 내장 전달기. X-SBX-04·05 · X-MKT-07 · X-RPT-07 후속.

## 잔여 P0·P1 (1차) — 2026-10-02 사용자 확인: "잔여 P1 감사 후 Epic 25~"

감사 방법: PRD FR 표 492건(P0 254 · P1 187 · P2 51)을 계획표·코드·시험의 FR ID 참조와 대조 → 미참조 P0 36 · P1 149 · P2 38, 이 중 코드 탐색으로 실제 미구현을 확인한 묶음을 우선(P0 → P1). 나머지 P1 은 다음 차수 후보.

| # | Epic | 기능(FR) | 수용 기준 / 시험 케이스 | 의존 | 차수 | 상태 |
|---|------|----------|-------------------------|------|------|------|
| 25 | 배포·릴리스 고도화 | PLT-DEP-05·06·07·08·09·10 · PLT-MGR-07·09 · PLT-DEP-03(매니페스트 렌더·드라이런) | 카나리(가중치·평가 게이트·자동 롤백)·블루그린 전환·롤백(이전 리비전)·환경 승격(dev→staging→prod, 승인)·드리프트 감지(선언 vs 실제)·배포 전 서명/정책 검사·스케일 제어(replicas)·MAS 토폴로지·k8s 매니페스트 생성 | M3 | 25th | DONE |
| 26 | MCP 고급 | MCP-HOST-03·04·05·08·10 · MCP-REG-04·07·08 · MCP-TOOL-06 · MCP-SEL-04 | OAuth 2.1 클라이언트(PKCE·토큰 저장·Keycloak 보호 MCP 서버)·게이트웨이 모드(단일 MCP 엔드포인트로 허용 서버 집계)·양방향 중계(sampling·elicitation → Gateway·승인함)·도구 호출 가드레일 훅·Origin 검사·서버 서명/출처 검증·버전 고정·승격·신뢰 메타·응답 크기 제한·toolset 프로필 | 25 | 26th | DONE |
| 27 | 가드레일·승인 안전 | GR-JB-02 · GR-PII-06·08 · GR-POL-06 · GR-IO-09 · AL-REF-06 · AL-EXIT-04 · X-HITL-03·04·05 · X-NTF-04·05 | 간접 인젝션 스캔(도구 결과·검색 문서)·역마스킹(권한·감사)·비밀 탐지·승인 화면 행위 diff·4-eyes·섀도 모드·거짓 성공 방지(검증 근거 없는 완료 거부)·예산 종료·승인 규칙 정책(조건→승인자 그룹)·라우팅·위임·SLA 에스컬레이션·알림 중복 억제·다이제스트·온콜 | 25 | 27th | DONE |
| 28 | 엔터프라이즈 IAM | PLT-IAM-03·05·07·09 · PLT-TEN-04 · X-WSP-08 | SCIM 2.0(사용자·그룹 프로비저닝 → 멤버십)·ABAC 조건(OpenFGA Conditions: 시간·환경·금액)·워크로드 신원(에이전트 단기 토큰 audience·SPIFFE 형식 ID)·MFA·세션 정책(acr·만료·동시 세션)·예산 상속(조직 → 프로젝트)·워크스페이스 할당량, 잔여 P0·P1 1차 게이트 | 25~27 | 28th | DONE |

잔여 1차 결과 (2026-10-03): DONE 4 · BLOCKED 0(Epic) — 25th 11/11 (local r2 · prod r2) · 26th 12/12 (local r3 · prod r1) · 27th 10/10 (local r4 · prod r2) · 28th 11/11 (local r1 · prod r3).
**잔여 1차 게이트 `test/impl/28th/gate.sh` — PASS (round 8 전체 345 + 분할 재실행 E9·E20 21)**: round 1~7 은 환경 용량(Docker 7.7 GiB·누적 리허설 볼륨)으로 회차마다 다른 기존 스위트가 부하 실패 → 2026-10-04 사용자 결정(16 GiB · 리허설 prod 볼륨 초기화 · 분할 허용). 게이트가 드러낸 실제 결함 5건 수정(OpenAPI→MCP 도구 노출, E27 미리보기 기본 강제 → opt-in, X-RPT WORM 봉인 경합, SIEM 수동 flush 미완, E25 정리) — [28th result](../test/impl/28th/result.md).
잔여 1차 가정: Kubernetes 는 렌더·드라이런만, cosign 대신 ed25519 매니페스트 서명, MCP OAuth 는 서비스 계정만(사용자 PKCE 후속), 가드 훅은 MCP 게이트웨이에서만, local(auth off) 은 quorum 1·ABAC/MFA/세션 효과 미검증(prod 에서 검증), SAML·SPIRE mTLS·storage_gb 집행 후속.
