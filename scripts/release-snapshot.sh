#!/usr/bin/env bash
# 로컬 릴리스 리허설 — 콘솔 빌드 → Manager 에 내장 → GoReleaser 스냅샷(게시·서명 없음) → dist/ 산출물. 원본 안내 페이지는 복원한다.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${VERSION:-$(cat VERSION)}"
UI=apps/manager/internal/ui/dist
backup=$(mktemp -d); cp -R "$UI/." "$backup/"
restore() { rm -rf "$UI"; mkdir -p "$UI"; cp -R "$backup/." "$UI/"; rm -rf "$backup"; }
trap restore EXIT
echo "== console build"
# 호스트(macOS) node_modules 는 익명 볼륨으로 가린다 — 리눅스용 의존성은 컨테이너 안에서 설치
docker run --rm -e CI=true -e COREPACK_ENABLE_DOWNLOAD_PROMPT=0 -v "$PWD/apps/console":/c -v /c/node_modules -w /c node:22-alpine \
  sh -c "corepack enable >/dev/null && pnpm install --frozen-lockfile --silent && pnpm build >/dev/null"
rm -rf "$UI"; mkdir -p "$UI"; cp -R apps/console/dist/. "$UI/"
echo "== goreleaser snapshot ($VERSION)"
docker run --rm -e GOTOOLCHAIN=auto -e ATLAS_VERSION="$VERSION" -v "$PWD":/repo -v prodai-atlas-local_gomod:/go/pkg/mod -w /repo/apps/manager \
  goreleaser/goreleaser:v2.12.7 release --snapshot --clean --skip=publish,sign,sbom,validate
ls -1 apps/manager/dist | grep -E "\.(tar\.gz|zip|deb|rpm|txt)$"
