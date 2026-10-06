# 시험 시나리오 / Test Scenario — impl 20th (Epic 20 Bitter Lesson)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` BL-MODEL-01~08 · BL-HARN-01~06 · BL-COMP-01·02·03·05·06
- 범위 결정 (2026-10-02, 사용자): M3 1차 · 보정 플래그 = OpenFeature flagd 컨테이너
- 실행: `e20_bl.py` 가 실제 Gateway(alias 오버레이·고정 배정·서빙 통계·계산 정책)·BL·EV·AL-PLAN·AL-RUN·flagd(OFREP)·NATS 를 호출. 다른 시험의 alias 를 흔들지 않도록 실행별 alias `e20-chat-<nonce>` 를 만들어 쓴다.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E20-01 | 카탈로그·후보 0%·평가·채택 규칙 (MODEL-01~04) | mock-chat → mock-chat-small 후보, 스위트 k=4 | 후보 weight 0 등록, 기준·후보 회귀, promote(같은 품질·저비용), 임시 alias 정리 | P0 |
| E20-02 | 카나리·고정 배정·승격 (MODEL-05·06) | 20% → 100%, 키 60개 × 2회 | 같은 키 같은 모델, 두 모델 서빙, 승격: 후보 100% + `<alias>-prev` 폴백 | P0 |
| E20-03 | 원클릭 롤백·감사 (MODEL-06) | 후보 롤백 | 등록 전 버전 복원, 감사 register·decide·promote·rollback | P0 |
| E20-04 | 비싼 후보 hold · 비용 SLO 자동 롤백 (MODEL-04·05) | mock-chat-large, force 카나리 50% | hold, force 없이 422, 비용 위반 rolled_back | P0 |
| E20-05 | 수명종료 스캔·모델 카드 (MODEL-07·08) | 폐기 44일 전 모델 | 사용 alias·대체(최저가 조건 충족) · 카드 Markdown | P1 |
| E20-06 | 계산 정책 (COMP-01·02·05) | effort auto · 추론 예산 3000 | easy→low · hard→high · 추론 토큰 헤더 · 80% 넘으면 high→medium | P0 |
| E20-07 | 프롬프트 레지스트리·게시 게이트 (HARN-01·06) | v1 정상 · v2 "Reply in prose" | v1 게이트 pass → production, v2 409 차단, production 유지 | P0 |
| E20-08 | 보정 플래그 flagd (HARN-04) | 군말 플래너 + JSON 복구 on/off | on COMPLETED · off FAILED(JSON) | P0 |
| E20-09 | ablation (HARN-05) | 복구·폴백 플래그 | 하나씩 끄면 removable(서로 보완), 전부 끄면 keep | P1 |
| E20-10 | 하네스 매니페스트·규칙 외부화 (HARN-02·03) | AL-RUN max_turns 1 · 환불 규칙 | 턴 한도로 종료 · require_approval | P0 |
| E20-11 | best-of-n · 품질-계산 곡선 (COMP-03·06) | 정규식 검증기 · effort 3종 | 첫 통과 조기 종료 · low 추천(같은 품질 최저 비용) | P1 |
| E20-12 | 이벤트 중계 | NATS PLT 스트림 | plt.bl.model.candidate_added·promoted·rolled_back·plt.bl.prompt.published | P1 |
| 단위 | go test(alias·계산 정책·mock) · pytest test_bl(8) | — | PASS | P0 |
