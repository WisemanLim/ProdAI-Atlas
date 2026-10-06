# dev-env 시나리오 / scenario — prodai-atlas (루트 통합 스택)

> 서비스 단독 시나리오: `apps/manager/test/dev-env/scenario.md` · `agents/runtime/test/dev-env/scenario.md`

| id | 목적 / purpose | 명령 / command | 기대 / expected | 우선 |
|----|----------------|----------------|-----------------|------|
| ENV-01 | 도구·설정 점검 | `make preflight` | `Preflight OK` | P0 |
| ENV-02 | local 기동 | `make local-all` | manager·agent-runtime·console·redis·nats running/healthy | P0 |
| ENV-03 | Manager 헬스 | `curl -fsS http://127.0.0.1:8080/health` | HTTP 200, `env=local` | P0 |
| ENV-04 | Agent runtime 헬스 | `curl -fsS http://127.0.0.1:8000/health` | HTTP 200 | P0 |
| ENV-05 | 콘솔 → Manager 프록시 | `curl -fsS http://127.0.0.1:5173/health/manager` | HTTP 200 (impl 5th 에서 경로 변경: `/api/*` 는 Manager `/api/*` 로 그대로 전달) | P0 |
| ENV-06 | NATS JetStream | `curl -fsS http://127.0.0.1:8222/jsz` | JSON, `config.store_dir=/data/jetstream` | P0 |
| ENV-07 | Redis | `docker compose -p prodai-atlas-local exec redis redis-cli ping` | `PONG` | P0 |
| ENV-08 | local DB | `make db-ping` · `make db-ping APP_SVC=agent-runtime` | `sqlite OK` ×2 | P0 |
| ENV-09 | 테스트 진입점 | `make test-all` | exit 0 (go test · pytest · vitest) | P0 |
| ENV-10 | prod 기동 (postgres) | `make prod-all` (CHANGE_ME 교체 후) | 전 서비스 + db healthy | P1 |
| ENV-11 | prod DB | `make db-ping ENV=prod` | `postgres OK` | P1 |
| ENV-12 | 정리 | `make local-stop && make prod-stop` | 컨테이너 없음 | P1 |

## 업종 추가 시험 (COMPLIANCE.md → test/impl 에서 구현)

| id | 항목 | 단계 |
|----|------|------|
| CMP-01 | 크로스테넌트 데이터 누출 차단(RLS) | 테넌트 모델 구현 후 |
| CMP-02 | OpenAPI 계약 적합성 | Manager API 구현 후 |
| CMP-03 | 감사로그 보관기간 경과 삭제 증명 | 감사로그 구현 후 |
