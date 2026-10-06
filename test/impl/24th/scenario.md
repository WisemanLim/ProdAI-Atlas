# 시험 시나리오 / Test Scenario — impl 24th (Epic 24 마켓·리포트 · M3 완료 게이트)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` X-MKT-01~06 · X-RPT-01~06·08 · 화면 S101(템플릿·마켓)·S102(사용량 리포트)
- 범위 결정 (2026-10-02, 사용자): M3 2차 Epic 22~24 (Epic 24 = 마켓·리포트 + M3 완료 게이트)
- 실행: `e24_mkt_rpt.py` 가 실제 X-MKT(`agent-mkt` 8023)·X-RPT(`agent-rpt` 8022)·Manager(승인함·비밀·알림 규칙·감사)·Gateway·X-SBX·MCP-REG·EV·MAT·가짜 업스트림(공개 MCP Registry·Splunk HEC)·Mailpit·NATS 를 호출.
  수집 경로 = Manager X-NTF 내장 전달기(`ATLAS_RPT_INGEST_URL`)가 PLT·GW·AUDIT 스트림을 HMAC 서명 webhook 으로 `agent-rpt /v1/ingest/events` 에 전달. Manager 감사는 별도 `AUDIT` 스트림 `audit.recorded` 로 흐른다.
- `e24_console.py` = Playwright(로컬 Chrome) S101·S102 E2E, prod 는 시험용 Keycloak 사용자를 만들고 지운다.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E24-01 | 카탈로그·필터·검증 (MKT-01·03) | /svc/mkt 목록 · kind 필터 | 번들 4종 · 대표 버전 = 최신 게시본 · 서명/SLSA/SBOM valid | P0 |
| E24-02 | 원클릭 설치 (MKT-02) | 잘못된 이름 · 정상 + 비밀 파라미터 | 422 · Manager 에이전트+배포 · 비밀은 Manager 비밀 참조(원문 없음) · 폼 password 위젯 | P0 |
| E24-03 | MCP 서버 템플릿 | weather-mcp | MCP-REG 등록·스캔 scanned | P1 |
| E24-04 | 공급망 검증 (MKT-03) | 악성 지시 · 미등록/등록 게시자 키 · log4j SBOM | 스캔 ≥70 신청 422 · untrusted_key → valid · CVE-2021-44228 차단 | P0 |
| E24-05 | 사내 게시 (MKT-04) | private 업로드 → 신청 → Manager 승인 | 승인함 mkt.publish · 승인 후 플랫폼 서명·게시 · 다른 프로젝트에 안 보임 | P0 |
| E24-06 | 업데이트·업그레이드 (MKT-05) | 1.0.0 설치 → 확인 → 업그레이드 · 회귀 실패 버전 | 1.1.0 알림 · 회귀 pass 후 PATCH(rev 2) · 실패 409 · 폐기 | P1 |
| E24-07 | MCP 카탈로그 (MKT-06) | 공개 Registry 동기화 · 검토 없이/검토 후 가져오기 | ≥2건 · 422 · MCP-REG approved(community) | P1 |
| E24-08 | 사용량 계측 (RPT-01) | Gateway 3건 · 샌드박스 1건 | requests ≥3 · tokens_in > 0 · sbx_cpu_s | P0 |
| E24-09 | 비용 리포트 (RPT-02) | project,model · KRW · CSV · PDF | 통화 환산 일치 · CSV 헤더 · PDF 1.4 | P0 |
| E24-10 | 통합 감사 (RPT-04) | Manager 비밀 PUT · 마켓 이벤트 · UPDATE 시도 | 해시 체인 검색 · 검증 valid · WORM 일치 · 보존 365 · 추가 전용 거부 | P0 |
| E24-11 | SIEM (RPT-05) | Splunk HEC 싱크 · 장애 · 복구 | OCSF 6003 수신 · 장애 시 버퍼·실패 계수 · 복구 후 0 | P1 |
| E24-12 | 차지백 (RPT-03) | 요율(마크업 10%)·플랜·월 마감 | 인보이스 1 · 부서 · 4.4 | P1 |
| E24-13 | 이상 탐지 (RPT-08) | 기준선 12창 + 급증 | 1건 · z > 3 · 원인 요청 id | P1 |
| E24-14 | 월간 보고서 (RPT-06) | 수신자 지정 | 4차트 HTML · PDF · Mailpit 메일(plt.rpt.report.ready) | P1 |
| E24-15 | 이벤트 | NATS PLT | plt.mkt.* · plt.rpt.* · plt.sbx.run.completed | P1 |
| E24-16 | 콘솔 S101·S102 | 브라우저 | 배지 · 폼 설치 · 설치본 · 비용 통화 전환 · 감사 체인 표시 · 런타임 오류 없음 | P0 |
| 단위 | pytest test_mkt(7)·test_rpt(6)·test_secrets(+1) · vitest schemaForm(3) · go test | — | PASS | P0 |
| 게이트 | `m3-gate.sh` — test-all·레드팀·prod 에서 Epic 1~24 스모크·콘솔 UI(v1·M2·M3·M3b)·하드닝 | — | rc=0 | P0 |
