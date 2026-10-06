#!/usr/bin/env python3
"""릴리스 채널 관리 (FR-PLT-REL-03): nightly → beta → stable.

승격은 재빌드 없이 같은 digest 를 새 태그로 다시 가리킨다(`docker buildx imagetools create`).
channels.json 이 채널별 버전·이미지 digest 의 정본이다.

  channels.py set <channel> <version>      # 레지스트리에서 digest 조회 후 기록 (CI 가 빌드 직후)
  channels.py promote <from> <to>          # nightly→beta, beta→stable 만 허용, digest 그대로 재태깅
  channels.py show
env: ATLAS_REGISTRY (기본 ghcr.io/prodai), CHANNELS_FILE (기본 deploy/channels.json)
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

IMAGES = ("atlas-manager", "atlas-gateway", "atlas-console", "atlas-agent-runtime", "atlas-sandbox")
ORDER = ("nightly", "beta", "stable")
REGISTRY = os.environ.get("ATLAS_REGISTRY", "ghcr.io/prodai")
FILE = os.environ.get("CHANNELS_FILE", os.path.join(os.path.dirname(__file__), "..", "deploy", "channels.json"))


def load():
    if os.path.exists(FILE):
        return json.load(open(FILE))
    return {"schema": 1, "registry": REGISTRY, "channels": {}}


def save(doc):
    with open(FILE, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")


def digest(ref):
    out = subprocess.run(["docker", "buildx", "imagetools", "inspect", ref],
                         check=True, capture_output=True, text=True).stdout
    m = re.search(r"^Digest:\s+(sha256:[0-9a-f]{64})", out, re.M)
    if not m:
        sys.exit(f"digest 없음: {ref}")
    return m.group(1)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_set(channel, version):
    doc = load()
    doc["channels"][channel] = {
        "version": version,
        "digests": {img: digest(f"{REGISTRY}/{img}:{version}") for img in IMAGES},
        "updated": now(),
    }
    save(doc)
    print(f"{channel} = {version}")


def cmd_promote(src, dst):
    if src not in ORDER or dst not in ORDER or ORDER.index(dst) != ORDER.index(src) + 1:
        sys.exit(f"승격은 {' → '.join(ORDER)} 한 단계씩만: {src} → {dst} 거부")
    doc = load()
    entry = doc["channels"].get(src)
    if not entry:
        sys.exit(f"{src} 채널 비어 있음")
    for img, d in entry["digests"].items():
        # 재빌드 금지: 같은 digest 를 채널 태그로 다시 가리킨다
        subprocess.run(["docker", "buildx", "imagetools", "create", "-t",
                        f"{REGISTRY}/{img}:{dst}", f"{REGISTRY}/{img}@{d}"], check=True)
        if digest(f"{REGISTRY}/{img}:{dst}") != d:
            sys.exit(f"digest 불일치: {img}")
    doc["channels"][dst] = {**entry, "promotedFrom": src, "updated": now()}
    save(doc)
    print(f"{src} → {dst}: {entry['version']} (digest 동일)")


def main(argv):
    if argv[:1] == ["set"] and len(argv) == 3 and argv[1] in ORDER:
        cmd_set(argv[1], argv[2])
    elif argv[:1] == ["promote"] and len(argv) == 3:
        cmd_promote(argv[1], argv[2])
    elif argv[:1] == ["show"]:
        print(json.dumps(load(), indent=2))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
