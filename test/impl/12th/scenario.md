# 시험 시나리오 / Test Scenario — impl 12th (Epic 12 패키징·릴리스)

- 근거: `.docs/ProdAI-Atlas_PRD.pdf` §11.15 PLT-PKG-01·02·03·07·08 · PLT-REL-01·02·03·04·05·08 · §11.17 데스크톱 · NFR 보안(비루트·서명·SBOM)
- 범위 결정 (2026-10-02, 사용자): Epic 11~12 모두 · 데스크톱(Tauri 2) **macOS 빌드까지**
- 원칙: 실제 레지스트리·GitHub 릴리스·PyPI 게시 없음. 레지스트리는 로컬 `registry:2`(localhost:5555), 서명은 리허설 전용 cosign 키쌍(CI 는 keyless). 시크릿은 리허설 값만.

| id | 목적 | 입력 | 기대 | 우선 |
|----|------|------|------|------|
| E12-01 | 이미지 하드닝 (PKG-01) | 4종 이미지 config · prod compose | 비루트(65532 / nginx 101), OCI 라벨(version·licenses·source), 우리 서비스 `read_only` + tmpfs `/tmp` 로 healthy | P0 |
| E12-02 | 멀티아치 (PKG-01) | `scripts/build-multiarch.sh` (buildx docker-container) → localhost:5555 | 4종 모두 linux/amd64 + linux/arm64 매니페스트 리스트, amd64 이미지 에뮬레이션 실행 | P0 |
| E12-03 | 단일 바이너리 (PKG-02·03) | `scripts/release-snapshot.sh` (GoReleaser v2 스냅샷) | `atlas`·`atlas-manager` 6타깃 아카이브(LICENSE 포함) + deb/rpm + checksums 검증, darwin 실행, manager 단독(SQLite) 콘솔 서빙 | P0 |
| E12-04 | Python wheel (PKG-07) | `uv build` → 깨끗한 python:3.12-slim 에 설치 | `atlas-agent serve --unit units.al_plan` 단독 기동 · Card · readyz(agent_core) · SendMessage COMPLETED · 패키지 내 마이그레이션 | P0 |
| E12-05 | compose 번들 + install.sh (PKG-08) | `deploy/bundle` → 빈 디렉터리, 이미지 전용(빌드 없음) | 16 서비스 healthy, Manager `/health` version 0.2.0·postgres, `/svc/*` 프록시, Manager 경유 A2A 루프 | P0 |
| E12-06 | `atlas install` (PKG-08) | `--values` 비대화 (compose 모드) | 번들 6파일 + `.env`(권한 600, CHANGE_ME 없음, 시크릿 무작위) + values 기록 | P0 |
| E12-07 | SemVer·호환·업그레이드 (REL-01·02·08) | `atlas backup` · `atlas upgrade --dry-run` · 다운그레이드 compat | pg_dump 백업 파일(600), dry-run 계획(코어 범위·마이그레이션·단계) 무변경, 다운그레이드 거부 exit 1 | P0 |
| E12-08 | 서명 (REL-04) | `e12_cosign.sh` | sign·SBOM attest·verify 성공, 다른 키·미서명 이미지 검증 거부 | P0 |
| E12-09 | SBOM·라이선스 (REL-05) | `scripts/sbom.sh` · `license-check.py` | 이미지 4 + 바이너리 2 × (SPDX + CycloneDX) = 12, 실제 SBOM 정책 통과, 합성 AGPL 라이브러리 거부(OS 패키지 GPL 은 허용) | P0 |
| E12-10 | 채널 (REL-03) | `scripts/channels.py` (nightly→beta→stable) | 승격은 digest 재태깅(재빌드 없음) · stable digest = 빌드 digest · 단계 건너뛰기 거부 | P0 |
| E12-11 | 릴리스 자동화 정의 (REL-01·04·05) | `release-please-config.json` · `.github/workflows/release.yml` | actionlint 0 오류. release-please(버전 마커) → GoReleaser · buildx · cosign keyless · SBOM attest · grype · 라이선스 · tauri-action · 채널/승격(서명 검증 후) | P1 |
| E12-12 | 데스크톱 macOS (§11.17) | `apps/desktop` Tauri 2 `tauri build --bundles app` | `.app` 생성(ad-hoc 서명), 실행 시 atlas-manager 사이드카가 127.0.0.1 임의 포트에서 `/health` 200·콘솔 서빙, 앱 종료 시 사이드카 종료 | P0 |
| E12-13 | 회귀 | `make test-all` · M1 최종 게이트(prod) | 전체 단위 시험 PASS, E1~E12 스모크 prod PASS | P0 |
