# 시험 시나리오 / Test Scenario — impl 27th (Epic 27 가드레일·승인 안전)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` GR-JB-02 · GR-PII-06·08 · GR-IO-09 · GR-POL-06 · AL-EXIT-04 · AL-REF-06 · X-HITL-03·04·05 · X-NTF-04·05
- 범위 결정 (2026-10-02, 사용자): 잔여 P1 감사 후 Epic 25~
- 실행: `e27_safety.py` 가 실제 GR-IO(8005)·AL-RUN(A2A, Manager 경유)·Gateway(mock 모델)·Manager(승인 정책·알림 규칙·이벤트)·NATS 를 호출. 웹훅 수신은 시험 프로세스가 호스트에 띄운 HTTP 서버.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E27-01 | 간접 인젝션 (GR-JB-02) | tool_result 경계 — `display:none` 지시문 · 제로폭 문자 · 본문 지시 청크 | indirect_injection·hidden_text 카테고리 · 숨김 제거 · `[quarantined:` · 정상 본문 유지 | P0 |
| E27-02 | 비밀 탐지 (GR-PII-08) | output 경계 — AWS 키 · `token=…` | `<SECRET_AWS_ACCESS_KEY>`·`<SECRET_GENERIC>` 라벨 · 원문 제거 | P0 |
| E27-03 | 가명 볼트·역마스킹 (GR-PII-06) | 같은 세션 두 요청 · LLM 변형 토큰 `< EMAIL_ADDRESS 2 >` · 권한 없음/있음 | 요청 간 같은 원문 = 같은 토큰 · 권한 없으면 거부 · 퍼지 매칭 복원 | P0 |
| E27-04 | 섀도 모드 (GR-IO-09) | 테넌트 정책에 shadow 단계 추가 | 201 · 차단 없음 · shadow 단계 1개 기록 | P1 |
| E27-05 | 임계값 튜닝 (GR-IO-09) | 라벨 6건 · max_fpr 0 | 제안 임계값 fpr 0 · recall ≥ 0.66 | P1 |
| E27-06 | 거짓 성공 방지·예산 예측 (AL-REF-06 · AL-EXIT-04) | require_external_verify 인사 목표 · max_tokens 200 무한 루프 | 3회 주장 후 MAX_TURNS · 넘기 전 `선제 종료` · 사용량 ≤ 200 | P0 |
| E27-07 | 승인 정책 (X-HITL-03·04 · GR-POL-06) | 미리보기 없는 고위험 · deny 규칙 · 금액 조건 require · evaluate API | 422 preview_required · 403 · 규칙명·quorum · required·previewRequired | P0 |
| E27-08 | 2인 승인·SLA·지표 (X-HITL-04·05) | (prod) 같은 결정자 2회 · 다른 키 결정 · SLA 3초 | 202 · 409 already_voted · approved · escalatedAt · stats | P0 |
| E27-09 | 알림 고도화 (X-NTF-04·05) | 같은 키 3회+다른 키 · 다이제스트 4건 · sev1 에스컬레이션 · ack | 웹훅 2 · 다이제스트 1(count 4) · primary 1 · secondary 1 · acked | P1 |
| E27-10 | 이벤트 | NATS PLT | plt.gr.secret_detected · plt.approval.escalated | P1 |
| 단위 | pytest test_guard_m4(6) · test_loop(+2) · go test approval_policy·notify | — | PASS | P0 |
