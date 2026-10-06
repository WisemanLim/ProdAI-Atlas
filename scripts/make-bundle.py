#!/usr/bin/env python3
"""compose 번들 생성 (FR-PLT-PKG-07) — 저장소 compose(prod) 를 빌드 없이 이미지 참조만 하는 설치 번들로 변환한다.

출력: deploy/bundle/{compose.yaml, otel/collector.yaml, keycloak/atlas-realm.json, openbao/init.sh, .env.example, install.sh(그대로), VERSION}
      + apps/manager/internal/bundle/files/ (atlas CLI 가 embed)
사용: python3 scripts/make-bundle.py [--version X.Y.Z]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "deploy/bundle"
EMBED = ROOT / "apps/manager/internal/bundle/files"
IMAGE = {"manager": "atlas-manager", "gateway": "atlas-gateway", "console": "atlas-console", "sandbox": "atlas-sandbox"}  # 그 외 우리 서비스 = atlas-agent-runtime


def render() -> dict:
    files = ["docker-compose.yml", "docker-compose.prod.yml", *sorted(str(p.relative_to(ROOT)) for p in (ROOT / "compose").glob("*.yml"))]
    cmd = ["docker", "compose", "--project-directory", str(ROOT)]
    for f in files:
        cmd += ["-f", str(ROOT / f)]
    cmd += ["--env-file", str(ROOT / ".env.example"), "config", "--no-interpolate", "--no-normalize"]
    return yaml.safe_load(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)


def transform(doc: dict) -> dict:
    doc.pop("name", None)
    for name, svc in doc["services"].items():
        if "build" in svc:
            svc.pop("build")
            svc["image"] = f"${{ATLAS_REGISTRY:-ghcr.io/prodai}}/{IMAGE.get(name, 'atlas-agent-runtime')}:${{ATLAS_VERSION:?ATLAS_VERSION 필요}}"
        if "env_file" in svc:
            svc["env_file"] = [".env"]
        vols = []
        for v in svc.get("volumes", []):
            if isinstance(v, str) and "collector.yaml" in v:
                v = "./otel/collector.yaml:/etc/otelcol/collector.yaml:ro"
            elif isinstance(v, dict) and "collector.yaml" in str(v.get("source", "")):
                v = {**v, "source": "./otel/collector.yaml"}
            elif isinstance(v, str) and "deploy/keycloak" in v:
                v = "./keycloak:/opt/keycloak/data/import:ro"
            elif isinstance(v, str) and "deploy/openbao" in v:
                v = "./openbao:/init:ro"
            elif isinstance(v, dict) and "deploy/openbao" in str(v.get("source", "")):
                v = {**v, "source": "./openbao"}
            elif isinstance(v, dict) and "deploy/keycloak" in str(v.get("source", "")):
                v = {**v, "source": "./keycloak"}
            vols.append(v)
        if vols:
            svc["volumes"] = vols
    return doc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=(ROOT / "VERSION").read_text().strip())
    a = ap.parse_args()
    doc = transform(render())
    (OUT / "otel").mkdir(parents=True, exist_ok=True)
    header = (f"# ProdAI Atlas compose 번들 v{a.version} — scripts/make-bundle.py 가 생성 (직접 수정 금지)\n"
              "# 설치: ./install.sh  ·  또는 atlas install\n")
    (OUT / "compose.yaml").write_text(header + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
    shutil.copy(ROOT / "deploy/otel/collector.yaml", OUT / "otel/collector.yaml")
    (OUT / "keycloak").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "deploy/keycloak/atlas-realm.json", OUT / "keycloak/atlas-realm.json")
    (OUT / "openbao").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "deploy/openbao/init.sh", OUT / "openbao/init.sh")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    env = re.sub(r"^ATLAS_VERSION=.*$", f"ATLAS_VERSION={a.version}", env, flags=re.M)
    env += "\n# 이미지 레지스트리 (에어갭: 사설 레지스트리 주소)\nATLAS_REGISTRY=ghcr.io/prodai\n"
    (OUT / ".env.example").write_text(env, encoding="utf-8")
    (OUT / "VERSION").write_text(a.version + "\n")
    core = re.search(r'CORE_VERSION = "([^"]+)"', (ROOT / "agents/runtime/atlas_agent_core/server.py").read_text()).group(1)
    store_dir = ROOT / "apps/manager/internal/store"
    mgr_mig = len(list((store_dir / "migrations").glob("0*.sql"))) + len([p for p in store_dir.glob("0*_*.go") if not p.name.endswith("_test.go")])
    rt_mig = sorted(p.name.split("_")[0] for p in (ROOT / "agents/runtime/migrations/versions").glob("0*.py"))[-1]
    compat = {"schema": 1, "versions": {a.version: {"manager": a.version, "gateway": a.version, "agentCore": f">={core} <1.0.0", "a2a": "1.0",
              "mcp": ["2025-11-25", "2026-07-28"], "migrations": {"manager": mgr_mig, "runtime": rt_mig}, "upgradeFrom": ">=0.1.0"}}}
    (OUT / "compat.json").write_text(json.dumps(compat, ensure_ascii=False, indent=2) + "\n")
    if EMBED.exists():
        shutil.rmtree(EMBED)
    shutil.copytree(OUT, EMBED)
    print(f"bundle v{a.version}: {OUT} ({len(doc['services'])} services) → embed {EMBED}")


if __name__ == "__main__":
    main()
