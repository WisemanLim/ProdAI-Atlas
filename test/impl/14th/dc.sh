#!/usr/bin/env bash
# compose 래퍼 — Makefile 과 같은 파일 목록. 사용: dc.sh <local|prod> <compose 인자…>
set -euo pipefail
cd "$(dirname "$0")/../../.."
m=$1; shift
EXTRA=(); for f in compose/*.yml; do EXTRA+=(-f "$f"); done
exec docker compose -p prodai-atlas-$m -f docker-compose.yml -f docker-compose.$m.yml "${EXTRA[@]}" --env-file .env.$m "$@"
