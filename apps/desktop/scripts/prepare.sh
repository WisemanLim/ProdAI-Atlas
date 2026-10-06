#!/usr/bin/env bash
# 데스크톱 사이드카 준비: 콘솔 빌드 → Manager 에 내장 → atlas-manager 를 Tauri externalBin 이름(<name>-<target-triple>)으로 빌드.
#   prepare.sh            호스트 타깃
#   prepare.sh universal  macOS universal (aarch64 + x86_64 → lipo)
set -euo pipefail
D="$(cd "$(dirname "$0")/.." && pwd)"; R="$(cd "$D/../.." && pwd)"
VERSION="${ATLAS_VERSION:-$(cat "$R/VERSION")}"
OUT="$D/src-tauri/binaries"; mkdir -p "$OUT"
UI="$R/apps/manager/internal/ui/dist"
backup=$(mktemp -d); cp -R "$UI/." "$backup/"
trap 'rm -rf "$UI"; mkdir -p "$UI"; cp -R "$backup/." "$UI/"; rm -rf "$backup"' EXIT

(cd "$R/apps/console" && { corepack enable >/dev/null 2>&1 || true; } && pnpm install --frozen-lockfile --silent && pnpm build >/dev/null)
rm -rf "$UI"; mkdir -p "$UI"; cp -R "$R/apps/console/dist/." "$UI/"

gobuild() { # <goos> <goarch> <out>
  (cd "$R/apps/manager" && CGO_ENABLED=0 GOOS=$1 GOARCH=$2 go build -trimpath \
    -ldflags="-s -w -X main.version=$VERSION" -o "$3" ./cmd/server)
}
if [ "${1:-}" = universal ]; then
  gobuild darwin arm64 "$OUT/atlas-manager-aarch64-apple-darwin"
  gobuild darwin amd64 "$OUT/atlas-manager-x86_64-apple-darwin"
  lipo -create -output "$OUT/atlas-manager-universal-apple-darwin" "$OUT"/atlas-manager-{aarch64,x86_64}-apple-darwin
else
  triple=$(rustc -vV | sed -n 's/^host: //p')
  ext=""; case "$triple" in *windows*) ext=".exe" ;; esac
  goos=$(go env GOOS); goarch=$(go env GOARCH)
  gobuild "$goos" "$goarch" "$OUT/atlas-manager-$triple$ext"
fi
ls -la "$OUT"
