#!/usr/bin/env bash
# 멀티아치 이미지 빌드·푸시 (FR-PLT-PKG-01): linux/amd64 + linux/arm64 매니페스트 리스트.
#   ATLAS_REGISTRY=localhost:5555 scripts/build-multiarch.sh [version]
# 빌더: docker-container 드라이버(buildx create --name atlas-multi --driver docker-container --driver-opt network=host)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-$(cat "$ROOT/VERSION")}"
REGISTRY="${ATLAS_REGISTRY:?ATLAS_REGISTRY 필요}"
BUILDER="${BUILDER:-atlas-multi}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

build() { # <image> <context> <dockerfile>
  echo "==> $REGISTRY/$1:$VERSION ($PLATFORMS)"
  docker buildx build --builder "$BUILDER" --platform "$PLATFORMS" --target runtime \
    --build-arg VERSION="$VERSION" -f "$3" -t "$REGISTRY/$1:$VERSION" --push "$2"
}
build atlas-gateway       "$ROOT/apps/gateway"   "$ROOT/apps/gateway/Dockerfile"
build atlas-manager       "$ROOT/apps"           "$ROOT/apps/manager/Dockerfile.bundle"
build atlas-console       "$ROOT/apps/console"   "$ROOT/apps/console/Dockerfile"
build atlas-agent-runtime "$ROOT/agents/runtime" "$ROOT/agents/runtime/Dockerfile"
build atlas-sandbox       "$ROOT/agents/sandbox" "$ROOT/agents/sandbox/Dockerfile"

for img in atlas-gateway atlas-manager atlas-console atlas-agent-runtime atlas-sandbox; do
  docker buildx imagetools inspect "$REGISTRY/$img:$VERSION" | grep -E "^Digest|Platform:" | grep -v unknown
done
