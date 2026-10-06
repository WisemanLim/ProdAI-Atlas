"""atlas-agent CLI (FR-PLT-PKG-02) — wheel 설치본에서 단위 에이전트를 바로 실행한다.

  uv tool install prodai-agents
  atlas-agent serve --unit units.al_plan --port 8000 [--db sqlite:////tmp/al-plan.db] [--public-url http://host:8000]
  atlas-agent units          # 내장 단위 목록
"""
from __future__ import annotations

import argparse
import importlib
import os
import pkgutil
import sys


def units() -> list[str]:
    import units as pkg
    out = []
    for m in pkgutil.iter_modules(pkg.__path__):
        for mod in (f"units.{m.name}", f"units.{m.name}.agent_spec"):
            try:
                if hasattr(importlib.import_module(mod), "agent"):
                    out.append(mod)
                    break
            except Exception:  # noqa: BLE001,S112 — 선택 의존성이 없는 단위는 건너뛴다
                continue
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas-agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--unit", required=True, help="예: units.al_plan · units.al_run · units.gr_io")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--db", default=None, help="DATABASE_URL (기본 sqlite:///./atlas-<unit>.db)")
    s.add_argument("--public-url", default=None)
    sub.add_parser("units")
    a = ap.parse_args(argv)
    if a.cmd == "units":
        print("\n".join(units()))
        return 0
    name = a.unit.split(".")[1] if "." in a.unit else a.unit
    os.environ.setdefault("DATABASE_URL", a.db or f"sqlite:///./atlas-{name}.db")
    os.environ.setdefault("DB_ENGINE", "sqlite" if os.environ["DATABASE_URL"].startswith("sqlite") else "postgres")
    os.environ["ATLAS_UNIT_MODULE"] = a.unit
    os.environ.setdefault("ATLAS_PUBLIC_URL", a.public_url or f"http://{a.host}:{a.port}")
    import uvicorn
    uvicorn.run("app.main:app", host=a.host, port=a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
