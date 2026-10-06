#!/usr/bin/env bash
# ProdAI Atlas compose 번들 설치 (FR-PLT-PKG-07) — 사전 점검 → .env 생성(시크릿 무작위) → 기동 → 헬스 확인
# 사용: ./install.sh [--project atlas] [--no-start]
set -euo pipefail
cd "$(dirname "$0")"
PROJECT=atlas; START=1
while [ $# -gt 0 ]; do case "$1" in --project) PROJECT="$2"; shift ;; --no-start) START=0 ;; *) echo "알 수 없는 인자 $1"; exit 2 ;; esac; shift; done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
command -v docker >/dev/null || { echo "FAIL: Docker 필요"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "FAIL: docker compose v2 필요"; exit 1; }
docker info >/dev/null 2>&1 || { echo "FAIL: Docker 데몬 미기동"; exit 1; }
cpus=$(docker info --format '{{.NCPU}}'); mem=$(( $(docker info --format '{{.MemTotal}}') / 1024 / 1024 / 1024 ))
say "Docker OK · CPU ${cpus} · RAM ${mem}GiB (권장 4 CPU · 8GiB)"

if [ ! -f .env ]; then
  say ".env 생성 — CHANGE_ME 를 무작위 시크릿으로 채운다"
  cp .env.example .env
  rnd() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }
  KEY="sk-atlas-prod-$(rnd 22)"; DBPW="$(rnd 32)"
  while IFS= read -r line; do
    case "$line" in
      ATLAS_GATEWAY_AL_PLAN_KEY=CHANGE_ME) echo "ATLAS_GATEWAY_AL_PLAN_KEY=$KEY" ;;
      DB_PASSWORD=CHANGE_ME) echo "DB_PASSWORD=$DBPW" ;;
      MANAGER_DATABASE_URL=*) echo "MANAGER_DATABASE_URL=postgresql://atlas:$DBPW@db:5432/atlas" ;;
      RUNTIME_DATABASE_URL=*) echo "RUNTIME_DATABASE_URL=postgresql+psycopg://atlas:$DBPW@db:5432/atlas" ;;
      OPENAI_API_KEYS=CHANGE_ME) echo "OPENAI_API_KEYS=" ;;
      ATLAS_MANAGER_BOOTSTRAP_KEY=CHANGE_ME) echo "ATLAS_MANAGER_BOOTSTRAP_KEY=atl_prod_$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 26)_$(rnd 32)" ;;
      ATLAS_A2A_SIGNING_KEY=CHANGE_ME) echo "ATLAS_A2A_SIGNING_KEY=$(openssl ecparam -name prime256v1 -genkey -noout 2>/dev/null | base64 | tr -d '\n')" ;;
      *=CHANGE_ME) echo "${line%%=*}=$(rnd 32)" ;;
      *) echo "$line" ;;
    esac
  done < .env.example > .env
  chmod 600 .env
fi
grep -v '^#' .env | grep -q CHANGE_ME && { echo "FAIL: .env 에 CHANGE_ME 남음"; exit 1; }
[ "$START" = 1 ] || { say "--no-start: 파일만 준비"; exit 0; }

say "기동: docker compose -p $PROJECT up -d"
docker compose -p "$PROJECT" --env-file .env -f compose.yaml up -d
port=$(grep -E '^MANAGER_PORT=' .env | cut -d= -f2); port=${port:-8080}
for _ in $(seq 1 120); do curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "http://127.0.0.1:$port/health" && echo
say "콘솔: http://127.0.0.1:$port/   ·   중지: docker compose -p $PROJECT -f compose.yaml down"
