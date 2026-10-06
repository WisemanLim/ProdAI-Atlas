# 시험 시나리오 / Test Scenario — impl 25th (Epic 25 배포·릴리스 고도화)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` PLT-DEP-03·05·06·07·08·09·10 · PLT-MGR-07·09
- 범위 결정 (2026-10-02, 사용자): 잔여 P1 감사 후 Epic 25~ (계획표 "잔여 P0·P1 (1차)")
- 실행: `e25_deploy.py` 가 실제 Manager(배포·롤아웃·정책·승인함·감사)·prober·EV·NATS 와 가짜 A2A 에이전트(`fake-upstream /agent/<name>/<ok|fail>/<version>`)를 호출. prod 는 oidc·RLS·PostgreSQL, 승격 요청은 부트스트랩 서비스 키(요청자 ≠ 승인자).

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E25-01 | 카나리 시작·가중 라우팅 (DEP-05) | steps 30·60·100 · 키 120개 · 같은 키 반복 | running·30% · 중복 롤아웃 409 · 대략 30% 분할 · 고정 배정 | P0 |
| E25-02 | 단계 전진·승격·standby 롤백 (DEP-05·07) | advance ×2 · primary rollback | 60% → promoted · 이전 primary = standby · 맞바꿈 | P0 |
| E25-03 | 오류율 자동 롤백 | /a2a 500 canary · 4회 호출 | 5xx · rolled_back(오류율) · 이후 primary 만 | P0 |
| E25-04 | 드리프트 (DEP-09) | 카드 version 9.9.9 | drift 기록·이벤트 · 중단 | P1 |
| E25-05 | 블루그린 (DEP-06) | green 0% → 전환 | 전환 전 primary 만 · promoted | P1 |
| E25-06 | 평가 게이트 | 통과 스위트 · 실패 스위트 | eval_passed → promoted · 409 평가 게이트 · rolled_back | P0 |
| E25-07 | 배포 정책 (DEP-10) | digest·레지스트리·ed25519 서명·승인 정책 | 위반 3건 422 · 서명 메시지 API | P0 |
| E25-08 | 환경 승격 (DEP-08) | dev → staging (승인 필요) · dev → prod | 승인함 deploy.promote → 승인 → staging rev 2 · 단계 건너뛰기 422 | P0 |
| E25-09 | 스케일·k8s 렌더 (MGR-07 · DEP-03) | replicas 3 · manifests | 선언 3(compose 미적용) · Deployment+Service · 비루트·읽기 전용·digest 이미지 | P1 |
| E25-10 | 토폴로지 (MGR-09) | env dev | 노드·관찰 간선(호출·오류 수) | P1 |
| E25-11 | 이벤트·감사 | NATS PLT · Manager 감사 | plt.deploy.rollout.* · rolled_back · drift · promoted · scaled · policy_denied · deploy.* 감사 | P1 |
| 단위 | go test rollouts_test(3) · pytest test_mcp(OpenAPI 도구 9 유지) | — | PASS | P0 |
