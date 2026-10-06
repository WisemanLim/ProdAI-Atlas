# 시험 시나리오 / Test Scenario — impl 18th (Epic 18 승인함·알림 + 콘솔 M2 · M2 완료 게이트)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` X-HITL-01·02·07 · X-NTF-01·02·03 · PLT-CON(콘솔 M2 화면)
- 범위 결정 (2026-10-02, 사용자): M2 2차 Epic 16~18 · 알림 채널 = 웹훅 + 이메일(Mailpit 컨테이너)
- 실행: `e18_hitl_ntf.py` 가 Manager agent-proxy 로 AL-RUN 을 실제 호출(HITL), 승인함 API 로 결정, Gateway 가상키·예산으로 임계 이벤트를 만들고 호스트 webhook 수신기(`host.docker.internal`)·Mailpit API 로 전달을 확인. `e18_console.py` 는 playwright-core + 호스트 Chrome 으로 콘솔 M2 화면(prod 는 Keycloak 로그인부터).

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E18-01 | 루프 HITL → 통합 승인함 (HITL-01) | AL-RUN `atlas_delete_all` 목표 | INPUT_REQUIRED, 승인함 source=loop·sourceRef=taskId·unit AL-RUN·risk high·payload calls·해시 64자 | P0 |
| E18-02 | 정렬·필터 (HITL-02) | 위험 low·critical·high(7200s)·high(300s) | critical → high(만료 임박) → high → low, risk 필터 | P0 |
| E18-03 | 수정 후 승인 → 같은 Task 재개 | decision=edit, arguments 교체 | status edited, Task COMPLETED, 런 이벤트 hitl_edit, 재결정 409 | P0 |
| E18-04 | 반려 (HITL-07) | decision=reject + 사유 | 루프 HITL_DENIED, 사유 기록(골든셋 후보 이벤트 `plt.approval.rejected`) | P0 |
| E18-05 | 감사 | approval.decide | 결정 감사 ≥2, detail 에 payload 해시, 체인 검증 valid | P0 |
| E18-06 | 규칙 검증 (NTF-01) | 메타데이터 주소 webhook · 서명 키 | 400 unsafe_url, 응답 secret `***` | P0 |
| E18-07 | 예산 임계 → 다채널·스로틀 (NTF-02·03) | 가상키 예산 0.00001$ + 큰 요청 | webhook 1건(HMAC 검증·threshold 80·X-Atlas-Event), 100% 는 스로틀, 이메일 1건(Mailpit, CRITICAL) | P0 |
| E18-08 | 쿼터 초과 알림 · 시험 발송 | agents.max=0 프로젝트에 에이전트 생성 | 429 + webhook(key agents.max), 시험 발송 test:sent | P0 |
| E18-09 | 만료 (HITL-01) | expiresInS=60 | 15초 주기 만료기 → expired, 같은 Task 에 deny → HITL_DENIED | P0 |
| E18-10 | 권한 (prod) | read 범위 키로 결정 · 남의 조직 목록 | 403 · 403 | P0 |
| E18-11 | 승인함 밖 재개 → 철회 | 같은 Task 에 직접 approve | Task COMPLETED, 승인 요청 withdrawn(resolved_in_task) | P1 |
| E18-12 | 콘솔 M2 화면 E2E | 승인함·조직 관리·A2A | 수정 후 승인 → 같은 런 완료, 규칙 추가·시험 발송·전달 기록, 멤버·API 키·비밀·쿼터 탭, 레지스트리 BM25 → 서명 카드, 런타임 오류 없음 | P0 |
| 단위 | go test(approvals·notify) · pytest(AL-RUN 승인함·수정 인자) · vitest(승인함 helper) | — | 전부 PASS | P0 |
| GATE | M2 완료 게이트 `m2-gate.sh` | test-all · 레드팀 · prod(oidc·RLS·OpenBao) Epic 1~18 스모크·SSO UI(v1·M2)·CLI·하드닝 | 전부 PASS | P0 |
