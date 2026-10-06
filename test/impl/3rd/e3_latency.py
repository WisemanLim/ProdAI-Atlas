#!/usr/bin/env python3
"""E3-14 게이트웨이 추가 지연 측정 — mock(0ms) 공급자, 순차 N회, keep-alive 연결. 사용: e3_latency.py <admin_token> [N] [gateway]"""
import http.client
import json
import statistics
import sys
import time
import urllib.parse

ADMIN = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 200
G = urllib.parse.urlparse(sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8090")
conn = http.client.HTTPConnection(G.hostname, G.port)


def call(method, path, body, token):
    conn.request(method, path, json.dumps(body), {"content-type": "application/json", "authorization": f"Bearer {token}"})
    r = conn.getresponse()
    return r.status, r.read()


_, raw = call("POST", "/admin/keys", {"tenant": "latency", "models": ["chat-default"]}, ADMIN)
key = json.loads(raw)["key"]
body = {"model": "chat-default", "messages": [{"role": "user", "content": "ping"}]}
for _ in range(20):  # warm-up
    call("POST", "/v1/chat/completions", body, key)
lat = []
for _ in range(N):
    t0 = time.perf_counter()
    st, _ = call("POST", "/v1/chat/completions", body, key)
    lat.append((time.perf_counter() - t0) * 1000)
    assert st == 200, st
lat.sort()
p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))]  # noqa: E731
print(f"N={N} p50={p(0.50):.2f}ms p95={p(0.95):.2f}ms p99={p(0.99):.2f}ms mean={statistics.mean(lat):.2f}ms max={lat[-1]:.2f}ms")
print("PASS E3-14 (p95 ≤ 15ms)" if p(0.95) <= 15 else "WARN E3-14 p95 > 15ms (참고치)")
