# 시험 결과 / Test Result — impl 24th (Epic 24 마켓·리포트 · M3 완료 게이트)

- 일자 Date: 2026-10-02
- 대상 Target: X-MKT 단위(`units/mkt` — 번들 카탈로그·서명/provenance/SBOM 검증·설치·사내 게시(승인함)·업데이트·MCP 카탈로그), X-RPT 단위(`units/rpt` — 계측·비용 리포트(CSV·PDF)·차지백·통합 감사(해시 체인·추가 전용 트리거·WORM)·SIEM(OCSF·Splunk HEC)·이상 탐지·월간 보고서), 마이그레이션 0013, Manager(감사 이벤트 `audit.recorded`·AUDIT 스트림·X-RPT 내장 전달기 `ATLAS_RPT_INGEST_URL`·서비스 키 `secrets:write`(svc- 접두)), SDK 비밀 조회 401 재발급, X-SBX 사용량 이벤트, 가짜 업스트림(공개 MCP Registry·Splunk HEC), 콘솔 S101 템플릿·마켓 · S102 사용량 리포트
- 환경 Env: Docker 29.8.0 · macOS Darwin 25.6.0 · Go 1.26 · Python 3.12 · PostgreSQL 17 · 호스트 8080 점유 → `MANAGER_PORT=18080`, 8021 점유 → X-MKT 8023

## 요약 / Summary

- 전체 Total: 16, 통과 Pass: 16, 실패 Fail: 0, 최종 Verdict: **PASS** (local round 5 · prod round 2 + 게이트 r3)
- 단위: pytest 182 passed(+14: test_mkt 7 · test_rpt 6 · test_secrets +1) · vitest 23 passed(+3 schemaForm) · go test 전 패키지 ok(+2: 전달기·서비스 키 범위) · ruff·tsc clean
- **M3 완료 게이트** `test/impl/24th/m3-gate.sh`: round 1 293 PASS / 6 FAIL → 수정 → round 2 300 PASS / 1 FAIL(E18-07) → Gateway 발행 순서 수정 → **round 3 301 PASS · rc=0**

## 케이스 / Cases

| id | local | prod | 비고 |
|----|-------|------|------|
| E24-01 | PASS | PASS | 번들 4종 · 대표 버전 1.1.0 · 서명/SLSA/SBOM valid |
| E24-02 | PASS | PASS | 422 · Manager 에이전트+배포 · 비밀 파라미터 → `svc-mkt-*` 비밀 |
| E24-03 | PASS | PASS | weather-mcp → MCP-REG scanned |
| E24-04 | PASS | PASS | 악성 지시 차단 · untrusted_key → valid · CVE-2021-44228 |
| E24-05 | PASS | PASS | 승인함 mkt.publish → 승인 → 게시·서명 · 가시성 |
| E24-06 | PASS | PASS | 1.1.0 알림 · 회귀 pass 업그레이드(rev 2) · 회귀 fail 409 · 폐기 |
| E24-07 | PASS | PASS | 공개 Registry 2건 · 검토 필수 · approved(community) |
| E24-08 | PASS | PASS | Gateway·샌드박스 사용량 수집 |
| E24-09 | PASS | PASS | KRW 환산 · CSV · PDF |
| E24-10 | PASS | PASS | Manager 감사 → 해시 체인 · WORM 일치 · 보존 365 · UPDATE 거부 |
| E24-11 | PASS | PASS | OCSF 6003 · 장애 버퍼 → 복구 후 0 |
| E24-12 | PASS | PASS | 인보이스(마크업 10%) 4.4 |
| E24-13 | PASS | PASS | z > 3 · 원인 요청 id |
| E24-14 | PASS | PASS | 4차트 HTML · PDF · Mailpit 메일 |
| E24-15 | PASS | PASS | plt.mkt.* · plt.rpt.* · plt.sbx.run.completed |
| E24-16 | PASS | PASS | 콘솔 S101·S102 (배지·폼 설치·설치본·통화 전환·감사 체인) |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 단위 | 업로드 응답 500 (DetachedInstance) | 커밋 후 만료된 행 접근 | 세션 안에서 응답 구성 | PASS |
| local 1 | `agent-mkt` 기동 실패 | 호스트 8021 을 외부 프로세스가 점유 | 기본 포트 8023 (`MKT_PORT`) | PASS |
| local 1 | MCP 템플릿 설치 500 `SecretUnavailable` | Manager 재시작 후 에이전트가 캐시한 단기 토큰 무효(401) — SDK 가 조용히 실패 | `SecretClient.fetch` 401 → 토큰 재발급 1회 재시도 (+ Manager 에 `ATLAS_MCP_REG_ADMIN_TOKEN` 전달) | PASS |
| local 2·3 | 통화 비교 불일치 · 카탈로그 대표 버전이 폐기본 | 두 호출 사이 창 변화(시험) · 소액 비용을 4자리 반올림 · 대표 버전 = 최대 semver | until 고정 · 비용 8자리 보존 · 대표 = 최신 게시본 | PASS |
| prod 1 | 설치 403 · 수집 규칙 생성 403 | 서비스 키 범위에 비밀·알림 규칙 쓰기 없음 | Manager 내장 X-RPT 전달기(규칙 불필요) · 서비스 키 `secrets:write`(자기 조직 `svc-` 접두만, 회전 불가) · 월간 메일 규칙은 조직 관리자(없으면 event_only) | PASS |
| prod 2 | E24-10 행위자 유형 · E24-12 이벤트 누락 | 게이트 토큰은 API 키(apikey) · 시험이 존재하지 않는 조직 t-N 사용 → 이벤트 중계 거부 | 시험 기대값·조직 수정 | PASS |
| 게이트 1 | E1-10 PLT 메시지 수 29≠5 | 감사 이벤트를 PLT 스트림에 실음 | 별도 `AUDIT` 스트림(`audit.recorded`) — notifier 가 PLT·GW·AUDIT 소비 | PASS (게이트 r2) |
| 게이트 1 | E7-03·04·05 서버 수 증가 | MCP 기본 시드에 커넥터·stdio 추가 · prod 볼륨의 `stdio-demo` 가 검색어 demo 와 충돌 | 기본 시드 원복, `seed --systems` 분리 · `io.prodai/stdio-echo` 로 개명 · 리허설 DB 의 시험 잔여 행 삭제 | PASS (게이트 r2) |
| 게이트 1 | E18-UI 규칙 삭제 버튼 클릭 실패 | 긴 규칙 이름으로 알림 규칙 표가 패널 밖으로 넘쳐 버튼이 가려짐 | 표에 `table-wrap` · E24 시험이 만든 규칙 정리 | PASS (게이트 r2) |
| 게이트 2 | E18-07 예산 알림 100% 가 80% 보다 먼저 (간헐) | Gateway 가 이벤트마다 goroutine 으로 발행 → 같은 정산의 50·80·100% 순서 뒤섞임 | 단일 순서 보장 발행 큐(버퍼 4096, 가득 차면 경고 후 버림) | PASS (게이트 r3) |
| 게이트 1 | E24-11 복구 후 버퍼 1246 | flush 1회 500건 · 버퍼 수에 필터 밖 행 포함 | 호출당 최대 20묶음 비움 · 필터 일치분만 버퍼로 계산 | PASS (게이트 r2) |

## 설계 메모

- 서명: 메시지 = `atlas-template/v1|sha256:<bundle>|sha256:<provenance>|sha256:<sbom>` 을 ed25519 로 — 한 서명이 번들·출처·SBOM 을 묶는다. 신뢰 키 = 플랫폼 키(데이터 볼륨 0600, KMS 교체 지점) + 관리자 등록 게시자 키. 설치 때마다 재검증(저장 후 변조 = digest 불일치 → 403).
- 게시: 승인함 요청은 MKT 에이전트 자신의 단기 토큰(source=mkt), 결정 반영은 주기 reconcile(30초) 또는 `POST /v1/templates/reconcile`.
- 수집: Manager notifier 가 PLT·GW·AUDIT 를 그대로 X-RPT 로(HMAC, 전달 기록 없음, 비동기). 원천 id 로 멱등이라 재전송·중복 규칙에도 안전.
- 감사: Manager 감사(자체 해시 체인)를 X-RPT 가 다시 체인(원 해시 `manager_hash` 보존) — 에이전트 관리 이벤트(승인·삭제권·마켓·샌드박스 한도…)와 한 곳에서 검색·SIEM.

## 가정 / 후속

- 템플릿 인덱스는 메타 DB(OCI/ORAS 아티팩트 저장은 후속), cosign·Rekor 대신 내장 ed25519 + 신뢰 키 목록(오프라인).
- WORM 은 데이터 볼륨의 봉인 세그먼트(0444 + sha256) — 운영은 S3 Object Lock. 보존 하한 365일(설정이 낮으면 365 로).
- Parquet 내보내기는 pyarrow 미포함으로 415, 외부 과금(Lago·Polar) 연동은 후속. X-MKT-07·X-RPT-07(P2) 제외.
- 서비스 키 `secrets:write` 는 같은 부트스트랩 키를 쓰는 모든 에이전트가 `svc-` 비밀을 만들 수 있음 — 단위별 서비스 키 분리는 후속.

## 첨부 / Logs

`logs/e24-local-r1..r5.txt` · `logs/e24-prod-r1..r2.txt` · `logs/m3-gate-r1.txt` · `logs/m3-gate-r2.txt` · `logs/shots-local/` · `logs/shots-prod/` · `logs/gate-ui-*`
