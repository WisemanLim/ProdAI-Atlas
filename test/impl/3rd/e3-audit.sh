#!/usr/bin/env bash
# E3-11 감사 해시 체인 검증 — 사용: e3-audit.sh local|prod
set -euo pipefail
m="${1:-local}"
EXTRA=(); for f in compose/*.yml; do EXTRA+=(-f "$f"); done   # compose/*.yml 전부 (Makefile 과 동일)
DC=(docker compose -p prodai-atlas-$m -f docker-compose.yml -f docker-compose.$m.yml "${EXTRA[@]}" --env-file .env.$m)
if [ "$m" = local ]; then verify=(go run ./cmd/server -verify-audit); else verify=(/server -verify-audit); fi
"${DC[@]}" logs --no-log-prefix --no-color gateway | "${DC[@]}" exec -T gateway "${verify[@]}"
