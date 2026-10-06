# 시험 결과 / Test Result — impl 1st · Epic 1 Manager 카탈로그

- 일자 Date: 2026-10-02
- 대상 Target: `apps/manager` (go-gin · Go 1.26 · GORM + goose · NATS JetStream)
- 커밋 Commit: — (git 저장소 아님)

## 요약 / Summary

- 전체 Total: 13, 통과 Pass: 13, 실패 Fail: 0, 최종 Verdict: **PASS** (round 1, 재시도 0)

## 케이스 / Cases

| id | 결과 result | 비고 note |
|----|-------------|-----------|
| E1-01 | PASS | catalog(47·중복 없음) · manifest(필드 오류 9종·해시 키 순서 무관) · api(생명주기·필터·OpenAPI 정합) |
| E1-02 | PASS | `migrations applied engine=sqlite` |
| E1-03 | PASS | 47 |
| E1-04 | PASS | 201, `layer=L1`(카탈로그로 채움), rev 1 |
| E1-05 | PASS | 422 `metadata.unitId`·`spec.artifacts.image`(digest 고정 강제) |
| E1-06 | PASS | 409 `already_exists` |
| E1-07 | PASS | 같은 spec → `changed=false`, rev 1 |
| E1-08 | PASS | rev 2, versions 2건 |
| E1-09 | PASS | 204 → 404 → 같은 이름 재등록 201 (부분 유니크 인덱스) |
| E1-10 | PASS | 스트림 `PLT` +5 (registered·updated·deleted·registered·deleted) |
| E1-11 | PASS | OpenAPI 3.1.0 |
| E1-12 | PASS | PostgreSQL 17: `make db-migrate ENV=prod` 후 E1-03~11 동일 통과, `ux_agents_project_name` 인덱스 존재 |
| E1-13 | PASS | `make test-all` exit 0 |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 bug | 원인 cause | 수정 fix | 재시험 retest |
|-------|----------|------------|----------|---------------|
| 0 (구현 중) | `go get` 실패: gorm postgres 드라이버·goose·nats.go 가 `go >= 1.25/1.26` 요구 | 스캐폴드 시드의 Go 1.24 가 2026-10 생태계보다 낡음 | `apps/manager` Dockerfile `golang:1.26`, go.mod `go 1.26` | 의존성 해석·전 시험 통과 |

## 첨부 / Logs

`logs/` (비커밋) · 재실행 스크립트: `e1-smoke.sh [manager_url] [nats_monitor_url]`
