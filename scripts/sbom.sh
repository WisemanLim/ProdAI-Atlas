#!/usr/bin/env bash
# SBOM 생성 (FR-PLT-REL-05): 이미지·바이너리마다 SPDX + CycloneDX. syft 는 컨테이너로 실행(호스트 설치 없음).
#   scripts/sbom.sh [version]   → dist/sbom/*.{spdx,cdx}.json + 라이선스 검사
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-$(cat "$ROOT/VERSION")}"
REGISTRY="${ATLAS_REGISTRY:-ghcr.io/prodai}"
SYFT="anchore/syft:v1.33.0"
OUT="$ROOT/dist/sbom"
mkdir -p "$OUT"

for img in atlas-manager atlas-gateway atlas-console atlas-agent-runtime; do
  ref="$REGISTRY/$img:$VERSION"
  echo "==> $ref"
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$OUT:/out" "$SYFT" \
    "docker:$ref" -q -o "spdx-json=/out/$img.spdx.json" -o "cyclonedx-json=/out/$img.cdx.json"
done

# GoReleaser 산출 바이너리(있으면)
BIN="$ROOT/apps/manager/dist"
if ls "$BIN"/atlas_linux_amd64*/atlas >/dev/null 2>&1; then
  for b in "$BIN"/atlas_linux_amd64*/atlas "$BIN"/atlas-manager_linux_amd64*/atlas-manager; do
    name="$(basename "$b")-linux-amd64"
    echo "==> $name"
    docker run --rm -v "$b:/bin/$name:ro" -v "$OUT:/out" "$SYFT" \
      "file:/bin/$name" -q -o "spdx-json=/out/$name.spdx.json" -o "cyclonedx-json=/out/$name.cdx.json"
  done
fi

ls -1 "$OUT"
python3 "$ROOT/scripts/license-check.py" "$OUT"/*.cdx.json
