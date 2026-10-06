#!/usr/bin/env python3
"""E11-02·03 — 내장 콘솔 서빙과 /svc 프록시. 사용: e11_http.py [manager_url]"""
import json
import re
import sys
import urllib.error
import urllib.request

M = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
failed = 0


def check(cid, got, want):
    global failed
    ok = got == want
    failed |= not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got!r}" + ("" if ok else f" want={want!r}"))


def get(path):
    try:
        with urllib.request.urlopen(M + path, timeout=15) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode()


st, _, html = get("/")
asset = re.search(r'/assets/[^"]+\.js', html)
st2, _, deep = get("/o/default/p/default/e/dev/runs")
st3, h3, _ = get(asset.group(0)) if asset else (0, {}, "")
st4, _, api404 = get("/api/v1/nope")
check("E11-02", (st, bool(asset), st2, deep == html, st3, "immutable" in h3.get("cache-control", ""), st4, "not_found" in api404),
      (200, True, 200, True, 200, True, 404, True))
paths = {"ob": "/svc/ob/v1/traces?since=3600&limit=1", "alrun": "/svc/alrun/runs?limit=1", "ev": "/svc/ev/v1/datasets",
         "gr": "/svc/gr/v1/guard/policies/default", "mcpreg": "/svc/mcpreg/v0.1/servers", "gw": "/svc/gw/health"}
codes = {k: get(p)[0] for k, p in paths.items()}
check("E11-03", (codes, get("/svc/evil/x")[0]), ({k: 200 for k in paths}, 404))
sys.exit(failed)
