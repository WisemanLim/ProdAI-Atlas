# 시험 결과 / Test Result — dev-env

- 일자 Date: 2026-10-02
- 대상 Target: prodai-atlas 루트 통합 스택 (custom = go-gin `apps/manager` + python-fastapi `agents/runtime` + React/Vite `apps/console` + Redis + NATS JetStream)
- 커밋 Commit: — (git 저장소 아님)
- 환경 Env: Docker 29.8.0 · Compose v5.5.1 · macOS (Darwin 25.6.0)

## 요약 / Summary

- 전체 Total: 12, 통과 Pass: 12, 실패 Fail: 0 (round 1의 실패 1건은 수정 후 round 2에서 통과), 최종 Verdict: **PASS**
- 업종 추가 시험(CMP-01~03)은 구현 대상이 아직 없어 dev-env 범위 밖. `test/impl/<Nth>` 에서 수행한다.

## 케이스 / Cases

| id | 결과 result | 비고 note |
|----|-------------|-----------|
| ENV-01 | PASS | `Preflight OK (DB_ENGINE local=sqlite, prod=postgres)`. `.env.prod` CHANGE_ME 경고는 정상 |
| ENV-02 | PASS | 5개 컨테이너 Up. manager·agent-runtime·nats healthy, redis·console은 헬스체크 없음 |
| ENV-03 | PASS | `:8080/health` → `{"env":"local","status":"ok","db":{"engine":"sqlite"}}` |
| ENV-04 | PASS | `:8000/health` → `db.status=ok` (SQLite에 실제 `SELECT 1`) |
| ENV-05 | PASS | `:5173/api/health` → manager 응답 (Vite proxy), `:5173/` → index.html |
| ENV-06 | PASS | `/jsz` JSON 응답, `store_dir=/data/jetstream` (시나리오 기대값을 실제 경로로 보정) |
| ENV-07 | PASS | `redis-cli ping` → `PONG` |
| ENV-08 | PASS | manager · agent-runtime 각각 `sqlite OK` |
| ENV-09 | PASS | go test `ok` · pytest `1 passed` · vitest `2 passed`. pytest 경고 1건(아래 참고) |
| ENV-10 | PASS (round 2) | 6개 컨테이너 Up. db·manager·agent-runtime·nats healthy. `:8000/health` → `engine=postgres, status=ok`, 콘솔 nginx `/api` 프록시 정상 |
| ENV-11 | PASS | `make db-ping ENV=prod` → `postgres OK` |
| ENV-12 | PASS | `make prod-stop && make local-stop` 후 남은 컨테이너 0개 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 bug | 원인 cause | 수정 fix | 재시험 retest |
|-------|----------|------------|----------|---------------|
| 1 | ENV-10 `make prod-all` 실패: console 이미지 `pnpm build` 에서 `TS2591: Cannot find name 'process'` | `apps/console/tsconfig.json` 이 `vite.config.ts` 를 포함하는데 `types` 에 Node 타입이 없음 (스캐폴드 수작업 누락). dev 모드는 tsc를 거치지 않아 local에서는 드러나지 않음 | `package.json` devDependencies에 `@types/node ^22` 추가, `tsconfig.json` `types` 에 `"node"` 추가 | round 2: `make prod-all` exit 0, console `pnpm test && pnpm build` 통과 (`built in 446ms`) |

## 참고 / Notes

- pytest 경고: `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2`. 기능 영향 없음. agent-runtime 테스트 의존성을 정리할 때 처리한다.
- 콘솔 runtime(nginx)은 임시 구성이다. PRD §16.1의 Manager embed.FS 내장은 implement 단계에서 한다.

## 첨부 / Logs

`logs/` 참고 (비커밋):
`01-preflight` · `02-local-all` · `02b-local-ps` · `03-05-health` · `06-nats-jsz` · `07-redis` · `08-db-ping` · `09-test-all` · `09-console.round2` · `10-prod-all`(round 1 실패) · `10-prod-all.round2` · `10b-prod-ps` · `10c-prod-health` · `11-db-ping-prod` · `12a-local-stop` · `12-stop`
