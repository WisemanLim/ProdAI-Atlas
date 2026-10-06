# atlas-gateway

> 생성: `wise-vibe` 플러그인 `wds-scaffold` · 프로파일 `go-gin` — Go 1.24 · Gin
> English: [README.en.md](README.en.md)

## 실행 모델

실행은 **docker compose 전용**이며 모드는 **local / prod** 두 가지다.

| 모드 | compose 파일 | env 파일 | DB | 이미지 스테이지 |
|------|--------------|----------|----|----------------|
| local | `docker-compose.yml` + `docker-compose.local.yml` | `.env.local` (커밋) | SQLite (`/app/data/app.db`) | `dev` (소스 바인드·핫리로드) |
| prod | `docker-compose.yml` + `docker-compose.prod.yml` | `.env.prod` (비커밋) | `sqlite` | `runtime` |

`compose/*.yml`(선택 인프라: redis·kafka 등)은 두 모드에 자동 포함된다.

## 사전 요구사항

- Docker Engine / Docker Desktop + Compose v2
- GNU Make

언어 툴체인은 호스트에 필요 없다(컨테이너 안에서 빌드·테스트).

## 빠른 시작

```bash
make preflight          # Docker·Compose·env 파일 점검
make local-all          # local 모드 기동 (SQLite)
curl http://127.0.0.1:8080/health
make test               # local dev 컨테이너에서 테스트
make local-stop
```

prod 모드(로컬 리허설 또는 대상 호스트):

```bash
cp .env.example .env.prod   # 없을 때만 — CHANGE_ME 를 Secret Manager 값으로 교체
make prod-all
make db-ping ENV=prod
make db-migrate ENV=prod
```

## Make 타겟

| 타겟 | 설명 |
|------|------|
| `make <mode>-all` | 전체 스택 기동 (`mode` = local \| prod) |
| `make <mode>-build` | 이미지 재빌드 후 기동 |
| `make <mode>-logs [SVC=]` | 로그 추적 |
| `make <mode>-stop` | 컨테이너 정리 (볼륨 유지) |
| `make <mode>-restart [SVC=]` | 재기동 |
| `make <mode>-ps` | 상태 |
| `make test` | local dev 컨테이너에서 테스트 |
| `make shell [ENV=]` | 앱 컨테이너 셸 |
| `make db-ping [ENV=]` | DB 연결 확인 |
| `make db-shell ENV=prod` | RDBMS 클라이언트 |
| `make db-migrate / db-seed [ENV=]` | 마이그레이션 / 시드 |
| `make db-reset / db-fresh` | local SQLite 초기화 (+시드) — prod 거부 |
| `make deploy` | prod 롤아웃 (CHANGE_ME 남아 있으면 거부) |
| `make preflight / help` | 점검 / 목록 |

## 환경 변수

| 변수 | local | prod |
|------|-------|------|
| `APP_ENV` | `local` | `prod` |
| `DB_ENGINE` | `sqlite` | `sqlite` |
| `DATABASE_URL` | SQLite 파일 | `db` 서비스 접속 문자열 |
| `DB_USER` / `DB_PASSWORD` / `DB_NAME` | — | RDBMS 계정 (CHANGE_ME → 주입) |
| `API_PORT`, `BIND_HOST` | 게시 포트 | 게시 포트 |

prod DB 엔진 변경: `wds-scaffold go-gin --db postgres|mysql|mariadb|sqlite --force` 로 재생성하거나
`.env.prod`·`docker-compose.prod.yml`·`Makefile` 의 `DB_ENGINE` 를 함께 바꾼다.

> 시크릿은 `.env.prod` 에 커밋하지 않는다. `.env.local` 은 시크릿이 없어 커밋한다(CI `make test`).

## 시험

`test/` 단일 트리 — `test/dev-env/`(환경 검증 1회), `test/impl/<Nth>/`(구현 차수별). 자세한 규칙은 `test/README.md`.
