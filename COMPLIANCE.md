# COMPLIANCE — 정보통신·SaaS (ICT / SaaS — 기본 베이스라인) (KSIC J)

> 자동 생성 (wds-scaffold). 근거: `domains/ict-saas.yaml` + PRD 보강 항목(표시 ★, `ProdAI-Atlas_PRD.pdf` §13·§14·§19).
> 규제는 변하므로 출처(references)로 재확인한다.

## 분류

| 체계 | 코드 |
|------|------|
| KSIC | J (중분류 58 · 59 · 60 · 61 · 62 · 63) |
| ISIC | J |
| NACE | J |
| NAICS | 51 |

## 한국 규제 (1순위)

| 이름 | 시행 | 영향 |
|------|------|------|
| 정보통신망법 | 상시 | 네트워크·정보보호 의무(§47 = ISMS 근거), 침해사고 신고 |
| 개인정보보호법(PIPA) | 상시 | PII 동의·최소화·유출 통지(§32-2). PRD: 가명처리 · 한국형 PII 인식기(GR-PII, §9.18) |
| ISMS-P | 3년 주기 | 대형 ICT 서비스는 의무. 접근로그·변경관리·암호화 증적 |
| CSAP(클라우드) | 2025 온라인 발급 | 공공·정부 클라우드에 판매할 때 IaaS/SaaS/DaaS 인증 |
| ★ AI 기본법 | 2026-01-22 | 생성형 AI 고지·표시 의무(→ 07 Guardrails), 고영향 AI의 사람 관리·감독·위험관리, 문서화 export(Markdown/PDF). 시행령 세부 기준(워터마크 기술 요건)은 PRD 부록 F에 미확인으로 남아 있음 |

## 국제 기준

- SOC 2 Type II (미국 SaaS, 6~12개월 통제)
- ISO/IEC 27001 (SOC 2와 통제 40~85% 중첩)
- GDPR (EU 고객·테넌트)
- ★ OWASP Top 10 for LLM (LLM01~10) · OWASP Agentic(ASI) — PRD §19.2 통제 매핑
- ★ EU AI Act Art.10(데이터) · Art.14(사람 감독) · Art.50(투명성 고지)

## 데이터 등급

| 데이터 | 등급 | 비고 |
|--------|------|------|
| 테넌트 데이터 | 민감 | 고객별 격리(RLS → 스키마 → DB). PRD: Org → Project → Env 4단 격리 |
| 고객 PII | 민감 | PIPA / GDPR |
| 인증·아이덴티티(RBAC) | **규제대상** | 접근통제. local/prod 리허설을 포함한 비운영 환경에 실데이터 반입 금지 |
| 감사로그·텔레메트리 | 내부 | SOC 2 / ISO 보관·삭제 정책. PRD: 감사로그는 PostgreSQL append-only + 해시 체인, 1년 이상 |
| ★ 프롬프트·응답 trace | 민감 | PII 포함 가능. 로그 정화 후 ClickHouse 30일(요약 1년) |
| ★ 비밀(API 키·모델 공급자 자격증명) | **규제대상** | OpenBao. `.env.prod` 는 CHANGE_ME, 커밋 금지 |

## 인프라 패턴

- 멀티테넌트 격리(RLS / 스키마 / DB-per-tenant). prod 는 PostgreSQL 하나를 공유하고 Manager 테이블 8종에 RLS+FORCE(요청 역할 `atlas_rls`)를 건다(impl 14th). namespace·dedicated 격리 수준은 조직 속성만 — 프로비저닝은 Helm(후속)
- 관측성: OTel Collector → 티어드 스토리지, 검증된 삭제(SOC 2 CC6.5 / ISO A.8.10)
- 피처플래그 · API 게이트웨이 · 수평 오토스케일 · 시크릿 관리
- 표준 CI/CD

stack_overrides 대비 이 프로젝트의 차이 (PRD 결정이 우선):

| 항목 | ict-saas 권고 | 이 프로젝트 | 근거 |
|------|---------------|-------------|------|
| frontend | Next.js + Tailwind/shadcn | React 19 + Vite SPA (Manager 내장) + Tauri 2 | PRD §16.1. 단일 바이너리·데스크톱·에어갭 |
| backend | NestJS 또는 FastAPI | Go(Gin) Manager + FastAPI 에이전트 런타임 | PRD §1.3 컨트롤/데이터 플레인 분리 |
| data | PostgreSQL + Prisma, Redis | PostgreSQL(GORM / SQLAlchemy) + Redis + **NATS JetStream** | PRD §14.4·§14.5 |
| database 엔진 | PostgreSQL | PostgreSQL (`--db postgres`) | 일치 |

M0 에서 추가됨: OTel Collector(`compose/otel-collector.yml`, file 싱크 — 검증용), AI Gateway(`apps/gateway`, 감사 해시 체인).
M1 1차에서 추가됨: GR-IO 가드레일(`agent-gr`, baseline-v1 정책 팩 자동 적용), MCP-REG 사설 레지스트리(`agent-mcp-reg`), Collector `transform/pii` 정화.
M1 2차에서 추가됨: ClickHouse(trace 30일 TTL), OB 관측(`agent-ob`), EV 평가(`agent-ev`).
M2 1차에서 추가됨: Keycloak(SSO·서비스 계정) · OpenFGA(RBAC) · OpenBao(prod 비밀 저장소) 컨테이너, PostgreSQL RLS, 조직별 해시 체인 감사 로그, 경제성(예산·캐시·배치).
M2 2차에서 추가됨: 다중 에이전트 5종 + 분석 샌드박스(`sandbox`, 내부 전용 네트워크·read-only·cap_drop ALL·전용 UID·rlimit), Mailpit(알림 이메일 캡처 — 개발·리허설 전용), 통합 승인함(결정 감사·payload 해시), 알림 규칙(HMAC 서명 webhook·SSRF 차단).
M3 1차에서 추가됨: BL(`agent-bl` — 모델 교체 감사·모델 카드·프롬프트 게시 게이트·긴급 우회 감사), OpenFeature flagd(보정 플래그), MAT(`agent-mat` — 성숙도 진단·월간 리포트), Gateway alias 오버레이·계산 정책(Redis), Manager 이벤트 중계.
M3 2차에서 추가됨: X-KB(`agent-kb` — pgvector·ACL 전파·메모리 격리·삭제권 전파 증명서), X-SBX(`agent-sbx` + `egress-proxy` — 송신 허용목록·사설/메타데이터 차단·산출물 비밀/PII 검사·실행 감사), MCP-SYS(`agent-mcp-sys` — 자격증명 브로커·읽기 전용 DB 역할·리소스 경계/마스킹), `mcp-sbx`(저신뢰 stdio MCP 격리), X-MKT(`agent-mkt` — ed25519 서명·SLSA provenance·SBOM 취약 구성요소 차단·악성 지시 스캔·게시 승인), X-RPT(`agent-rpt` — 통합 감사 해시 체인·추가 전용 트리거·WORM 봉인 세그먼트·보존 ≥365일·SIEM OCSF), 시험 전용 `fake-upstream`.
M1 3차에서 추가됨: Manager 내장 콘솔 + `/svc/*` 프록시(허용목록·쿠키 제거), 릴리스 공급망(cosign 서명·SBOM·라이선스 정책·grype), 설치 번들·`atlas` CLI(시크릿 무작위 생성·`.env` 600), Tauri 데스크톱(루프백 전용 사이드카). 아직 없음: S3 호환 객체 저장.


## 개발환경 특수 요구

- 테넌트 격리 테스트(크로스테넌트 누출 단언, RLS 강제)
- 표준 CI/CD(lint / type / test / build / deploy)
- 텔레메트리 보관·삭제 검증(CC6.5 / A.8.10), OpenAPI 계약 테스트
- SOC 2 / ISO 통제 증적 자동화(접근로그·변경관리)

## 추가 시험

`test/dev-env/scenario.md` 의 CMP-* 와 연결되고, `test/impl/<Nth>/` 에서 구현한다.

| 항목 | 상태 (2026-10-02, M0 + M1 + M2 + M3 + 잔여 P0·P1 1차 Epic 25~28) |
|------|------------------------|
| OpenAPI 계약 적합성 (CMP-02) | **시행** — Manager·Gateway 라우트 ↔ `openapi.json` 1:1 단위 시험 (impl 1st·3rd). Manager 명세에 operationId 부여(impl 7th) |
| 크로스테넌트 누출 차단 (CMP-01) | **시행** — Manager API 조직 범위 + PostgreSQL RLS(모르는 tenant 0행·남의 행 0·교차 INSERT 거부, impl 14th E14-04·16), `/svc` 프록시 tenant 강제, A2A 테넌트 JWT 검증(불일치 거부·감사). 런타임(Python) 테이블 RLS 는 후속 — 현재 tenant 파티션 |
| 감사로그 보관기간 경과 삭제 (CMP-03) | **부분** — trace·span 은 ClickHouse TTL 30일 자동 삭제(impl 9th). Manager 감사는 조직별 해시 체인·append-only 트리거·일 단위 머클 루트·JSONL 내보내기(impl 14th). X-RPT 통합 감사 = 해시 체인 + UPDATE/DELETE 거부 트리거 + WORM 봉인 사본(0444·sha256) + 보존 하한 365일 + SIEM(OCSF) 전송(impl 24th). 오브젝트 스토리지 Object Lock·보관 경과 삭제 증명·외부 앵커(Rekor)는 후속 |
| 공급망 (SLSA · OWASP LLM03) | **시행** — 마켓 템플릿 설치 전 ed25519 서명·SLSA provenance(subject digest·신뢰 builder)·SBOM 필수, 취약 구성요소·악성 지시(정적 스캔 ≥70) 차단, 저장 후 변조(digest 불일치) 차단, 사내 게시는 승인함(X-HITL) 승인 후 플랫폼 키 서명(impl 24th). cosign·Rekor 투명성 로그·KMS 서명 키는 후속 |
| 코드 실행·송신 통제 (OWASP LLM05·ASI05) | **시행** — 샌드박스 기본 네트워크 차단, 실행별 egress 정책(허용목록·사설/메타데이터 차단·결정 로그), CPU·메모리·시간 상한·초과 이벤트, 산출물 비밀/PII 검사, 저신뢰 stdio MCP 는 비루트·read-only·env 비움 격리(impl 23rd). gVisor·microVM 은 런타임 미가용 — 요청 시 422 |
| Fail-static 72h | **부분** — Manager 중지 중 에이전트 동작·재등록 (impl 2nd), MCP-REG 장애 시 내장 도구로 계속 (impl 7th). 72h 카오스 시험은 후속 |
| PII 최소화·유출 방지 (PIPA) | **시행(규칙 기반)** — 입력·출력·도구 결과 경계 한국형 PII 마스킹·일관 가명(impl 8th), trace 저장 전 Collector 정화(E8-10), span 본문 기록 정책 기본 redacted(impl 9th), 골든셋 승격 시 마스킹 재적용(impl 10th), 판정 로그 원문 미보관. 이름·주소 NER 은 후속 |
| 비밀 누출 차단 (OWASP LLM02) | **시행** — span 본문의 API 키·토큰 형식 제거 + `gw.secret_leak_detected` 이벤트 (impl 9th). 가드 경계 비밀 인식기 7종(개인키·AWS·GitHub·Slack·OpenAI·JWT·엔트로피 기반 generic) → `<SECRET_*>` 라벨 + `plt.gr.secret_detected` (impl 27th) |
| OWASP LLM01 Prompt Injection · ASI01 | **부분** — 규칙 기반 탐지 + spotlighting + 레드팀 게이트. 간접 인젝션: 도구 결과·검색 문서의 숨김 텍스트 제거·지시 청크 격리, MCP 게이트웨이 오염 실행의 외부 쓰기 차단(lethal trifecta, impl 26th·27th). **holdout ASR 0.5 · FPR 0.33 (한계)** — 분류 모델 M2 |
| OWASP LLM06 Excessive Agency · ASI09 | **시행** — 비가역 도구 HITL(AL-ACT·GR-POL·MCP 승인 규칙), 정책 PDP 도구 인가, 루프 종료 조건·예산. 통합 승인함: operator 이상·요청자 본인 승인 불가·결정 해시 체인 감사·만료 시 deny (impl 18th). 다중 에이전트: 서브에이전트 읽기 전용 강제·쓰기 단일화·중첩 깊이 제한 (impl 17th) |
| 격리 코드 실행 (OWASP LLM05 · ASI05) | **시행** — 분석 코드는 전용 샌드박스 서비스: 외부 네트워크 없음·read-only rootfs·cap_drop ALL·no-new-privileges·전용 UID·CPU/메모리/프로세스/파일 rlimit·시간 한도 초과 = 실패 (impl 17th) |
| SSRF 차단 (OWASP A10) | **시행** — A2A push·알림 webhook 은 https + 공인 주소만, 사설 주소는 허용 호스트 env 로만 (impl 16th·18th) |
| OWASP MCP03·MCP09 (tool poisoning · Shadow MCP) | **시행** — 설치 전 스캔(≥70 거부), 도구 고정(rug pull 차단), 허용목록 기본 거부 (impl 7th) |
| 공급망 무결성 (SOC 2 CC8.1 변경관리 · ISO A.8.30 · SLSA) | **시행(CI 정의 + 로컬 리허설)** — 이미지 digest cosign 서명(CI keyless, 리허설은 키쌍 sign·verify·거부 시험), CycloneDX SBOM attest, 승격 전 서명 검증, 재빌드 없는 digest 승격 (impl 12th). 실제 게시·Rekor 기록은 첫 태그 시 |
| SBOM·라이선스 정책 | **시행** — 이미지·바이너리별 SPDX + CycloneDX, AGPL·SSPL·BUSL 등 거부(OS 패키지는 집계만), grype critical 실패 (impl 12th) |
| 컨테이너 하드닝 (CIS Docker) | **시행** — 비루트 UID 65532(콘솔 nginx 101), prod `read_only` rootfs + tmpfs, Docker 소켓 미마운트, HEALTHCHECK (impl 12th) |
| 비밀 생성·보관 (설치) | **시행** — `atlas install`·`install.sh` 가 시크릿 무작위 생성(서비스 API 키·A2A 서명 키 형식 포함), `.env` 권한 600, CHANGE_ME 잔존 시 거부 |
| 비밀 관리 (OWASP ASI03 · ISMS-P 암호키 관리) | **시행** — SecretStore(file·OpenBao KV v2), 관리자 write-only, 에이전트는 매니페스트 선언 비밀만 단기 토큰으로 수신, env 비밀 기본 차단, 모든 read·회전 감사, 공급자 키 무중단 회전(검증·24h 유예·90일 주기), OpenBao 서비스별 정책·루트 토큰 폐기 (impl 15th). auto-unseal·HA·BYOK 는 후속 |
| 접근통제·인증 (ISMS-P · SOC 2 CC6.1) | **시행** — OIDC SSO(웹 PKCE·CLI 장치 코드), 서비스 계정, API 키(Argon2id·범위·만료 ≤365일·폐기), OpenFGA 역할 템플릿, 권한 거부 감사, 조직 정지 시 에이전트 호출 401 (impl 14th). SCIM 2.0 프로비저닝(비활성화 = 이전 토큰 무효·API 키 즉시 폐기·그룹 튜플 제거), ABAC 조건(업무 시간·IP·MFA·금액, OpenFGA Conditions), 관리 작업 MFA(acr ≥ 2, RFC 9470 step-up), 세션 유휴·최대 수명·동시 세션 상한, 워크로드 신원(SPIFFE 형식 ID·peer audience)·RFC 8693 위임 범위 축소 (impl 28th). SAML 연합·K8s SPIRE mTLS 는 후속 |
| 배포 변경 관리 (SOC 2 CC8.1 · ISMS-P 변경관리) | **시행** — 배포 전 정책(digest 고정·허용 레지스트리·ed25519 매니페스트 서명·승인 필수), 카나리·블루그린(평가 게이트·오류율 자동 롤백), 환경 승격 = 승인함, 드리프트 감지 이벤트 (impl 25th). 승인 정책: 고위험 미리보기 필수·금지 규칙·quorum 2인 승인(1인 1표·요청자 제외)·위임·SLA 에스컬레이션 (impl 27th) |
| 예산·할당량 (FinOps · 서비스 가용성) | **시행** — 조직 → 프로젝트 → 환경 예산 상속(하위 합 ≤ 상위, Gateway 집행), 요금제 한도(에이전트 수·동시 실행·월 토큰) 생성·실행 게이트, 80·100% 이벤트 (impl 28th). storage_gb 는 표시만 |
| 모델 변경 관리·문서화 (EU AI Act Art.11·12 기술 문서 · AI 기본법 · SOC 2 CC8.1) | **시행** — 모델 교체는 후보 0% 등록 → 회귀 평가(pass^k) → 채택 규칙 → 카나리(SLO 자동 롤백) → 전환, 전 단계 BL 감사 + alias 버전 이력·롤백, 모델 카드 Markdown export (impl 20th). 프롬프트·하네스 production 게시는 회귀 게이트, 긴급 우회는 관리 토큰 + 사유 + 감사 |
| 평가 신뢰성 (LLM-as-Judge 편향·일치도) | **시행** — 사람 일치도 κ ≥ 0.6 미만 judge 는 게이트 사용 불가, pairwise 위치 교차, 배심원, 루브릭 개정 shadow 재채점 (impl 19th). 편향 완화 옵션(JUDGE-07)은 후속 |
| 생성형 AI 고지·표시 (AI 기본법 · EU AI Act Art.50) | **미시행** — 후속 |


- 크로스테넌트 데이터 누출 차단(RLS)
- OpenAPI 계약 적합성(contract test)
- 감사로그 보관기간 경과 후 삭제 증명
- ★ Fail-static: Manager가 멈춰도 데이터 플레인이 72시간 이상 동작 (NFR-AVL-02, 카오스 시험)
- ★ 생성형 AI 고지·표시 적용 여부 (AI 기본법 / EU AI Act Art.50)

## 출처

- https://isms-p.or.kr/sysm/intro/selectSysmCertDetail.do (ISMS-P)
- https://isms.kisa.or.kr/main/csap/notice/ (CSAP 2025)
- https://secureframe.com/blog/soc-2-vs-iso-27001
- https://oneuptime.com/blog/post/2026-02-06-telemetry-retention-soc2-iso27001/view
- https://www.ideadope.com/roadmaps/how-to-build-multi-tenant-saas-2025
- ★ `.docs/ProdAI-Atlas_PRD.pdf` v1.0 (2026-09-30) §13 비기능 · §14.5 저장소 배치 · §19.2 규제·표준 매핑 · 부록 F
