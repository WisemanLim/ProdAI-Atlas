# dev-env 시나리오 / scenario — atlas-manager (go-gin)

| id | 목적 / purpose | 명령 / command | 기대 / expected | 우선 |
|----|----------------|----------------|-----------------|------|
| ENV-01 | 도구·설정 점검 | `make preflight` | `Preflight OK` | P0 |
| ENV-02 | local 기동 | `make local-all` | 컨테이너 running/healthy | P0 |
| ENV-03 | 헬스 체크 | `curl -fsS http://127.0.0.1:8080/health` | HTTP 200, `env=local` | P0 |
| ENV-04 | local DB | `make db-ping` | `sqlite OK` | P0 |
| ENV-05 | 테스트 진입점 | `make test` | exit 0 | P0 |
| ENV-06 | prod 기동 (postgres) | `make prod-all` | app + db healthy | P1 |
| ENV-07 | prod DB | `make db-ping ENV=prod` | `postgres OK` | P1 |
| ENV-08 | 정리 | `make local-stop && make prod-stop` | 컨테이너 없음 | P1 |
