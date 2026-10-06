# 시험 시나리오 / Test Scenario — impl 16th (Epic 16 A2A 3종)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` A2A-CARD-01·02·03·06 · A2A-CLI-01·02·03·04·06·07 · A2A-SRV-03·05
- 범위 결정 (2026-10-02, 사용자): M2 2차 Epic 16~18
- 실행: SDK `atlas_agent_core.a2a_client.A2AClient` 로 실제 Manager(서명·레지스트리·agent-proxy)·에이전트를 호출. push 수신기는 호스트에서 띄우고 에이전트가 `host.docker.internal` 로 닿는다(`ATLAS_PUSH_ALLOW_HOSTS`).

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E16-01 | 카드 게시·서명 (CARD-01·02) | 모든 ready 에이전트 `GET /api/v1/agents/{id}/card` | Manager(Go) JCS + JWS(ES256, kid) 서명을 SDK(Python) 가 검증, 변조 카드 거부, capabilities streaming·push·extended, securitySchemes | P0 |
| E16-02 | skill 검색 (CARD-03·06) | `q` BM25 · `skill` 필터 | "계획 plan 분해" → al-plan 1위, skill=loop.run → al-run, trust=signed (가시성은 Go 시험) | P0 |
| E16-03 | 원격 위임 blocking (CLI-02·03) | skill 만 지정 | 레지스트리 해석 → 카드 검증 → Manager 경유 → COMPLETED(단계 3) | P0 |
| E16-04 | 스트림 (CLI-04 · SRV-02) | mode=stream | SendStreamingMessage SSE 를 Manager 가 중계, COMPLETED | P0 |
| E16-05 | async + 추적 (CLI-04) | mode=async | submitted → GetTask 폴링 COMPLETED, 원격 Task 미러 | P0 |
| E16-06 | INPUT_REQUIRED 되묻기 (CLI-06) | 비가역 도구 루프 | INPUT_REQUIRED → 같은 taskId 로 승인 재개 → COMPLETED | P0 |
| E16-07 | 마감·취소 (CLI-07) | timeout 0.05s | CancelTask → CANCELED, reason=timeout | P0 |
| E16-08 | Push (SRV-03) | `CreateTaskPushNotificationConfig` | 종료 시 webhook 수신, JWT(ES256, aud=URL, task_id) 를 에이전트 JWKS 로 검증, X-A2A-Notification-Token | P0 |
| E16-09 | Push SSRF 차단 | 메타데이터 주소 · 허용 밖 http | -32602 | P0 |
| E16-10 | 확장 카드 · 보안 스킴 (CARD-01 · SRV-05) | Manager 경유 · 직접 무토큰 | atlas.extended=true · -32051 | P0 |
| E16-11 | 중첩 깊이 (MA-ORCH-10) | depth=2 | DepthExceeded | P0 |
| E16-12 | 단위 | go test(서명 카드·JCS·레지스트리 BM25·가시성·에이전트 주체) · pytest 9(스트림·구독·Push CRUD·서명·재시도·클라이언트 위임·변조·타임아웃·재개) | 전부 PASS | P0 |
