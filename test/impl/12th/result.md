# 시험 결과 / Test Result — impl 12th (Epic 12 패키징·릴리스 · M1 최종 게이트)

- 일자 Date: 2026-10-02
- 대상 Target: v0.2.0 — 이미지 4종 · `atlas`/`atlas-manager` 바이너리 · `prodai-agents` wheel · compose 번들 · 릴리스 자동화 · Tauri 데스크톱(macOS)
- 커밋 Commit: — (git 저장소 아님)
- 환경 Env: Docker 29.8.0 (buildx v0.33 docker-container 빌더) · macOS Darwin 25.6.0 arm64 · Go 1.26.4 · Rust 1.98.1 · Node 22.23 · GoReleaser v2.12.7 · syft v1.33.0 · cosign v2.4.1 · actionlint 1.7.7 (도구는 컨테이너 실행, Rust 만 호스트)
- 레지스트리: 로컬 `registry:2` (localhost:5555, 시험 후 제거). 실제 GHCR·PyPI·GitHub 게시 없음.

## 요약 / Summary

- 전체 Total: 13, 통과 Pass: 13, 실패 Fail: 0, 최종 Verdict: **PASS** (round 2 — 실패 1건 수정 후 재시험)
- M1 최종 게이트 `m1-final-gate.sh`: **108 PASS · 0 FAIL** (test-all: go 10 패키지 ok · pytest 112 passed/1 skipped · vitest 13 passed, 레드팀, prod 스모크 E1~E12 106건)

## 케이스 / Cases

| id | 결과 | 비고 / 근거 로그 |
|----|------|------------------|
| E12-01 | PASS | prod 11개 서비스 `ReadonlyRootfs=true`·비루트(65532, 콘솔 101)·running, 이미지 config 라벨 version 0.2.0·licenses Apache-2.0 (양 아키텍처) — `m1-final-gate.txt`, `E12-multiarch-verify.txt` |
| E12-02 | PASS | 4종 모두 linux/amd64 + linux/arm64 매니페스트 리스트. amd64 manager·agent-runtime 에뮬레이션 실행 `x86_64`. Go·Node 빌드 스테이지 크로스 컴파일 — `E12-multiarch.txt` |
| E12-03 | PASS (round 2) | 6 아카이브(LICENSE·README 포함) + deb/rpm 4 + checksums 10/10 OK. `atlas 0.2.0-snapshot (bundle 0.2.0)` — `E12-snapshot-r2.txt` |
| E12-04 | PASS | 깨끗한 python:3.12-slim 에 wheel 설치 → `atlas-agent serve` 단독, card=al-plan · agent_core 0.2.0 · SendMessage COMPLETED — `E12-wheel.txt` |
| E12-05 | PASS | 번들(이미지 전용) 16 서비스 Up(헬스체크 있는 12개 healthy), `/health` postgres·0.2.0, `/svc/*` 6종 200·미등록 404, Manager 경유 루프 E6-02·03·04·06 — `E12-install-sh.txt`, `E12-bundle-*.txt` |
| E12-06 | PASS | `atlas install --values` → 6파일 + `.env`(600, CHANGE_ME 0) + values 기록 — `E12-install-cli.txt` |
| E12-07 | PASS | `atlas backup` → `backups/atlas-0.2.0-…sql` 108,663 B (600). upgrade dry-run: 코어 범위·마이그레이션·5단계 계획, "변경 없음". 0.1.5 다운그레이드 거부 exit 1 — `E12-backup.txt`, `E12-upgrade-*.txt` |
| E12-08 | PASS | `e12_cosign.sh` 6/6: sign · CycloneDX attest · verify · verify-attestation(컴포넌트 포함) · 다른 키 거부 · 미서명 거부 — `E12-cosign.txt` |
| E12-09 | PASS | SBOM 12개(이미지 4 + 바이너리 2 × SPDX/CycloneDX), 실제 SBOM 정책 통과, 합성 AGPL 라이브러리 거부 exit 1 · OS 패키지 GPL 허용 — `E12-sbom.txt`, `E12-release.txt` |
| E12-10 | PASS | `e12_release.sh`: nightly 기록 → nightly→stable 직행 거부 → beta → stable, stable digest = 빌드 digest(4종), channels.json promotedFrom=beta — `E12-release.txt` |
| E12-11 | PASS | actionlint release·ci·eval-gate 0 오류 (eval-gate 미사용 변수 경고 수정) — `E12-actionlint.txt` |
| E12-12 | PASS | `ProdAI Atlas.app` 29 MiB · Signature=adhoc · 사이드카 포함. `e12_desktop.sh` 10/10: `/health` ok·desktop·sqlite·0.2.0, 콘솔·API 200, 127.0.0.1 전용 리슨, 사이드카 부모 = 앱, 앱 데이터 DB 생성, quit 후 앱·사이드카 모두 종료. 창이 콘솔 개요 맵으로 전환되는 것 육안 확인(전체 화면 캡처에 개인 화면이 찍혀 파일 삭제) — `E12-desktop*.txt` |
| E12-13 | PASS | `m1-final-gate.sh` 108 PASS, rc=0 — `m1-final-gate.txt` |

## 발견 오류 및 수정 / Bugs & Fixes

| round | 오류 | 원인 | 수정 | 재시험 |
|-------|------|------|------|--------|
| 1 | GoReleaser `globbing failed for pattern ../../LICENSE` | 아카이브 `files` 는 프로젝트 밖 경로 glob 불가 (직전 실행은 files 추가 전이라 통과) | `before.hooks` 에서 `cp ../../LICENSE ../../README.en.md .`, 상대 경로 사용, `apps/manager/.gitignore` | round 2 PASS |
| 1 (예방) | 번들 `install.sh` 가 주석의 CHANGE_ME 로 실패 | `grep CHANGE_ME .env` 가 주석까지 검사 | `grep -v '^#'` 후 검사, 번들 재생성 + 스냅샷 재빌드로 CLI 내장본 갱신 | E12-05·06 PASS |
| — | 데스크톱 사이드카가 8080 고정·전 인터페이스 바인딩 | Manager 리슨 주소 하드코딩 | `ATLAS_LISTEN` env 추가(기본 `:8080`, env 3종 + 번들) | E12-12 PASS |
| — | 사이드카 GIN debug 로그 | `GIN_MODE` 미설정 | 사이드카 env `GIN_MODE=release` | r2 로그 확인 |
| — | 멀티아치 빌드 에뮬레이션 비용 | Go·Node 빌드를 타깃 아키텍처에서 실행 | `FROM --platform=$BUILDPLATFORM` + `GOOS/GOARCH` | E12-02 PASS |

## 참고 / Notes

- SBOM 의 "라이선스 미기재" 5,447건은 Go 모듈(바이너리에 라이선스 메타 없음)·일부 pip 메타데이터 한계. 거부 판정은 기재된 라이선스 기준이며, CI 에서는 grype 와 함께 돈다.
- OS 패키지(debian python-slim)의 GPL 은 베이스 이미지 몫으로 집계만 한다.
- cosign keyless(Fulcio·Rekor)·실제 게시·Windows/Linux/universal 데스크톱·Apple 공증은 `release.yml` 정의만 있고 첫 태그 때 CI 에서 실행된다.
- 리허설 키·비밀번호는 임시값(`rehearsal-only-not-secret`), 키 파일은 시험 후 삭제.
- 시험 중 `/tmp/atlas-bundle-e2e` 번들 스택·볼륨, 로컬 레지스트리, buildx 빌더는 모두 정리됨(남은 컨테이너 0).

## 재실행 / Rerun

```bash
docker run -d --name atlas-reg -p 5555:5000 registry:2
docker buildx create --name atlas-multi --driver docker-container --driver-opt network=host
ATLAS_REGISTRY=localhost:5555 scripts/build-multiarch.sh && scripts/sbom.sh
bash test/impl/12th/e12_cosign.sh && bash test/impl/12th/e12_release.sh
bash test/impl/12th/e12_wheel.sh && bash test/impl/12th/e12_desktop.sh   # 데스크톱은 .app 빌드 후
bash test/impl/12th/m1-final-gate.sh
```

## 첨부 / Logs

`logs/` (비커밋): E12-install-cli · E12-install-sh · E12-bundle-{ps,http,loop} · E12-backup · E12-upgrade-{dry,downgrade} · E12-snapshot-r2 · E12-sbom · E12-multiarch · E12-multiarch-verify · E12-cosign · E12-release · E12-wheel · E12-actionlint · E12-desktop-build(-r2) · E12-desktop(-r2) · m1-final-gate · gate-*
