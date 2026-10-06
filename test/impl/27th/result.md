# 시험 결과 / Test Result — impl 27th (Epic 27 가드레일·승인 안전)

- 일자 Date: 2026-10-02
- 대상 Target: GR-JB(간접 인젝션 `scan_indirect`·숨김 텍스트·청크 격리·sanitize), GR-PII(비밀 인식기 7종·`label` 연산자·`plt.gr.secret_detected`), GR-IO(가명 볼트 AES-GCM `guard_vault`·`POST /v1/guard/reveal`(권한 `pii:reveal`·퍼지 토큰)·섀도 단계·`POST /v1/guard/tune`), GR-POL(`shadow` 모드), AL-EXIT(입력/출력 분리 상한·다음 호출 예측 선제 종료), AL-REF(`require_external_verify`), AL-RUN(`action_preview`·호출별 `usage` 이벤트), Manager 승인 정책(`/orgs/{org}/approval-policy`·`/approvals/evaluate`·quorum·위임·SLA 에스컬레이션·`/approvals-stats`), X-NTF(중복 억제 창·다이제스트·온콜 에스컬레이션 단계·ack), 마이그레이션 0015 · 00010·00011
- 환경 Env: Docker 29.8.0 · macOS Darwin 25.6.0 · PostgreSQL 17 · Keycloak · OpenFGA v1.10.2 · NATS JetStream

## 요약 / Summary

- 전체 Total: 10, 통과 Pass: 10, 실패 Fail: 0, 최종 Verdict: **PASS** (local round 4 · prod round 2)
- 단위: pytest 198 passed(+8: test_guard_m4 6 · test_loop 2) · go test ok(approval_policy · notify `TestDedupDigestAndEscalation`) · ruff clean

## 케이스 / Cases

| id | local | prod | 비고 |
|----|-------|------|------|
| E27-01 | PASS | PASS | 숨김 지시·제로폭 제거 · 지시 청크 `[quarantined:` · 정상 본문 유지 |
| E27-02 | PASS | PASS | AWS·generic 토큰 → `<SECRET_*>` |
| E27-03 | PASS | PASS | 세션 간 같은 토큰 · 권한 없으면 거부 · 변형 토큰 복원 |
| E27-04 | PASS | PASS | 섀도 단계 기록 · 결정 영향 없음 |
| E27-05 | PASS | PASS | fpr 0 · recall ≥ 0.66 제안 |
| E27-06 | PASS | PASS | 외부 검증 없는 완료 3회 → MAX_TURNS · 선제 종료(사용량 ≤ 상한) |
| E27-07 | PASS | PASS | 422 preview_required · 403 deny · 규칙·quorum · evaluate |
| E27-08 | PASS | PASS | (prod) 202 → 409 already_voted → 200 approved · SLA escalatedAt · stats |
| E27-09 | PASS | PASS | dedup 2 · digest 1(count 4) · primary 1 · secondary 1 · acked |
| E27-10 | PASS | PASS | plt.gr.secret_detected · plt.approval.escalated |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 단위 | E8-02 회귀 위험 | 기준 정책 버전 `1.0.0` 고정 시험 | 새 체커 대신 기존 jailbreak·spotlight 단계 안에서 경계별 동작 | PASS |
| 단위 | 볼트 행 DetachedInstanceError | 세션 종료 후 속성 접근 | `expire_on_commit=False` | PASS |
| 단위 | `action_preview` 예외 | OpenAI tool_call arguments 는 JSON 문자열 | 문자열이면 파싱 후 요약 | PASS |
| 단위 | 알림 지문 경쟁 | 공유 필드 `pendingFP` 동시 사용 | `preDeliver` 반환값으로 전달 | PASS |
| local 1 | E27-02 generic 미탐 | 키워드에 `token` 없음 | 키워드 추가 | PASS |
| local 2 | E27-06 선제 종료 안 됨 (호출 토큰 0) | 같은 목표 반복 → exact cache 적중(비용 0) — 정상 동작 | 시험 목표에 실행별 난수 | — |
| local 3 | E27-06 선제 종료 안 됨 | 첫 호출 기준 예측이 다음 입력에 더해질 관측(도구 결과)을 빠뜨림 | 예측 = max(추세, 직전 + 출력 + 새 관측 ≈4자/토큰) | PASS |
| prod 1 | E27-08 403 self_approval | 시험이 요청·결정을 같은 키로 — 4-eyes 정상 차단 | 요청 = 에이전트 토큰, 2차 결정자 = 이름 지정 `svc` 키(`user:apikey:{id}`) | PASS |

## 설계 메모

- 간접 인젝션은 tool_result·retrieval 경계에서만 청크 분류(지시 청크 격리) — input 경계는 기존 탐지 유지.
- 볼트 키 = HKDF(ATLAS_GUARD_SECRET, tenant) · 세션 단위 AES-GCM. 역마스킹은 `pii:reveal` 권한 + 감사 이벤트, 다른 테넌트 볼트는 열리지 않음.
- 승인 정책: 고위험(high·critical)은 `action_preview` 필수(도구 호출이면 자동 생성), deny 규칙은 403, require 규칙은 승인자(`role:`·`user:`)·quorum·SLA·에스컬레이션 대상을 승인 요청에 고정. 승인자 1인 1표(유니크 제약), 결정 서명 = sha256(주체|시각|payload 해시|결정).

## 가정 / 후속

- auth off(local) 에서는 모든 주체가 같아 quorum 2 시험 불가 — local 은 quorum 1, prod 에서 2인 승인 검증.
- 루프 예산 예측은 토큰 근사(≈4자/토큰) — 정확한 토크나이저는 후속.

## 첨부 / Logs

`logs/e27-local-r1..r4.txt` · `logs/e27-prod-r1..r2.txt`
