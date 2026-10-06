"""egress 프록시 (FR-X-SBX-02 · FR-MCP-SYS-04) — 샌드박스·MCP 서버의 외부 송신을 허용목록으로만 통과시킨다.

  :3128  HTTP 프록시 (CONNECT 터널 + 평문 HTTP 전달). 클라이언트는 Proxy-Authorization: Basic base64(<policy-id>:<token>)
  :8080  관리 API — PUT /policies/{id} {token, allow[], ttl_s} · DELETE /policies/{id} · GET /log?policy= · GET /health
         (X-Egress-Admin 헤더 = EGRESS_ADMIN_TOKEN)

판정: 정책 허용목록(호스트 · *.도메인)에 있어야 하고, 해석된 주소가 사설·루프백·링크로컬(169.254.169.254 메타데이터 포함)이면
허용목록에 정확히 그 호스트가 있을 때만 통과(내부 가짜 업스트림 시험용). 정책 없는 요청은 거부. 모든 결정은 로그에 남는다.
"""
import asyncio
import base64
import ipaddress
import json
import os
import socket
import time

ADMIN = os.environ.get("EGRESS_ADMIN_TOKEN", "")
STATIC = {p.split("=", 1)[0]: {"token": p.split("=", 1)[1].split(":")[0], "allow": p.split("=", 1)[1].split(":", 1)[1].split("|"), "exp": None}
          for p in os.environ.get("EGRESS_STATIC_POLICIES", "").split(";") if "=" in p and ":" in p}
POLICIES: dict[str, dict] = dict(STATIC)
LOG: list[dict] = []


def allowed(policy: dict | None, host: str) -> tuple[bool, str]:
    if policy is None:
        return False, "no_policy"
    if policy.get("exp") and policy["exp"] < time.time():
        return False, "policy_expired"
    pats = policy["allow"]
    hit = any(host == p or (p.startswith("*.") and host.endswith(p[1:])) for p in pats)
    if not hit:
        return False, "not_in_allowlist"
    try:
        addrs = {ipaddress.ip_address(i[4][0]) for i in socket.getaddrinfo(host, None)}
    except (socket.gaierror, ValueError):
        return False, "dns_fail"
    internal = any(a.is_private or a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast for a in addrs)
    meta = any(str(a) in ("169.254.169.254", "fd00:ec2::254") for a in addrs)
    if meta:
        return False, "metadata_address"
    if internal and host not in pats:            # 사설 주소는 와일드카드로 열 수 없다
        return False, "private_address"
    return True, "allowed"


def auth(headers: dict[str, str]) -> tuple[str | None, dict | None]:
    raw = headers.get("proxy-authorization", "")
    if not raw.lower().startswith("basic "):
        return None, None
    try:
        pid, tok = base64.b64decode(raw[6:]).decode().split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return None, None
    p = POLICIES.get(pid)
    return (pid, p) if p and p["token"] == tok else (pid, None)


async def read_head(reader):
    data = await reader.readuntil(b"\r\n\r\n")
    lines = data.decode("latin-1").split("\r\n")
    method, target, _ = lines[0].split(" ", 2)
    headers = {k.lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
    return method, target, headers, data


async def pipe(r, w):
    try:
        while chunk := await r.read(65536):
            w.write(chunk)
            await w.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        w.close()


async def proxy(reader, writer):
    try:
        method, target, headers, raw = await read_head(reader)
    except (asyncio.IncompleteReadError, ValueError, asyncio.LimitOverrunError):
        writer.close()
        return
    pid, pol = auth(headers)
    if method == "CONNECT":
        host, _, port = target.rpartition(":")
    else:
        from urllib.parse import urlsplit
        u = urlsplit(target)
        host, port = u.hostname or "", str(u.port or 80)
    ok, why = allowed(pol, host)
    LOG.append({"ts": time.time(), "policy": pid, "method": method, "host": host, "port": port, "decision": "allow" if ok else "deny", "reason": why})
    del LOG[:-2000]
    if not ok:
        writer.write(f"HTTP/1.1 403 Forbidden\r\nX-Egress-Reason: {why}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        writer.close()
        return
    try:
        ur, uw = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 10)
    except (OSError, asyncio.TimeoutError):
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    if method == "CONNECT":
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
    else:   # 평문 HTTP: 절대 URL → 경로로 바꿔 전달, 프록시 인증 헤더 제거
        from urllib.parse import urlsplit
        u = urlsplit(target)
        first, rest = raw.split(b"\r\n", 1)
        rest = b"\r\n".join(ln for ln in rest.split(b"\r\n") if not ln.lower().startswith(b"proxy-authorization"))
        uw.write(f"{method} {u.path or '/'}{'?' + u.query if u.query else ''} HTTP/1.1\r\n".encode() + rest)
        await uw.drain()
    await asyncio.gather(pipe(reader, uw), pipe(ur, writer))


async def admin(reader, writer):
    try:
        method, target, headers, _ = await read_head(reader)
        n = int(headers.get("content-length", 0))
        body = json.loads(await reader.readexactly(n)) if n else {}
    except (asyncio.IncompleteReadError, ValueError):
        writer.close()
        return

    def send(code, obj):
        b = json.dumps(obj).encode()
        writer.write(f"HTTP/1.1 {code} X\r\nContent-Type: application/json\r\nContent-Length: {len(b)}\r\nConnection: close\r\n\r\n".encode() + b)

    path = target.split("?")[0]
    if path == "/health":
        send(200, {"status": "ok", "policies": len(POLICIES)})
    elif not ADMIN or headers.get("x-egress-admin") != ADMIN:
        send(401, {"error": "admin token"})
    elif path.startswith("/policies/") and method == "PUT":
        pid = path.rsplit("/", 1)[1]
        POLICIES[pid] = {"token": body["token"], "allow": list(body.get("allow") or []), "exp": time.time() + float(body.get("ttl_s", 900))}
        send(200, {"policy": pid, "allow": POLICIES[pid]["allow"]})
    elif path.startswith("/policies/") and method == "DELETE":
        POLICIES.pop(path.rsplit("/", 1)[1], None)
        send(200, {"deleted": True})
    elif path == "/log":
        q = dict(p.split("=", 1) for p in target.split("?", 1)[1].split("&")) if "?" in target else {}
        send(200, {"items": [x for x in LOG if not q.get("policy") or x["policy"] == q["policy"]][-500:]})
    else:
        send(404, {"error": "no route"})
    await writer.drain()
    writer.close()


async def main():
    s1 = await asyncio.start_server(proxy, "0.0.0.0", 3128)
    s2 = await asyncio.start_server(admin, "0.0.0.0", 8080)
    async with s1, s2:
        await asyncio.gather(s1.serve_forever(), s2.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
