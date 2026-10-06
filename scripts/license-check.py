#!/usr/bin/env python3
"""라이선스 검사 (FR-PLT-REL-05): CycloneDX JSON SBOM 의 컴포넌트 라이선스를 정책과 대조.

  license-check.py sbom1.cdx.json [sbom2.cdx.json ...]
거부 목록(강한 copyleft·소스공개 상용 제한)이 하나라도 있으면 exit 1.
OS 패키지(apk·deb)는 컨테이너 베이스 이미지 몫이라 거부 대상에서 제외하고 집계만 한다.
"""
import json
import re
import sys
from collections import Counter

DENY = re.compile(r"\b(AGPL|SSPL|BUSL|BSL-1\.1|Commons-Clause|Elastic-2\.0|CC-BY-NC)", re.I)
OS_PURL = ("pkg:apk/", "pkg:deb/", "pkg:rpm/")


def licenses(comp):
    out = []
    for item in comp.get("licenses", []):
        if "expression" in item:
            out.append(item["expression"])
        lic = item.get("license", {})
        out.append(lic.get("id") or lic.get("name") or "")
    return [x for x in out if x]


def main(paths):
    if not paths:
        sys.exit(__doc__)
    denied, counts, unknown = [], Counter(), 0
    for path in paths:
        doc = json.load(open(path))
        for comp in doc.get("components", []):
            lics = licenses(comp)
            purl = comp.get("purl", "")
            if not lics:
                unknown += 1
            for lic in lics:
                counts[lic] += 1
                if DENY.search(lic) and not purl.startswith(OS_PURL):
                    denied.append(f"{path}: {comp.get('name')}@{comp.get('version')} — {lic}")
    print("상위 라이선스:", ", ".join(f"{k}={v}" for k, v in counts.most_common(8)))
    print(f"라이선스 미기재 컴포넌트: {unknown}")
    if denied:
        print("FAIL 거부 라이선스:")
        print("\n".join("  " + d for d in denied))
        return 1
    print("OK 거부 라이선스 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
