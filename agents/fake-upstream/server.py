"""가짜 업스트림 (시험·개발 전용) — 실자격증명 없이 외부 시스템 연동을 검증한다.

  GET /docs/<name>.html                      지식베이스 URL 원천 (X-KB-01)
  /github/*  /slack/*  /jira/*              표준 커넥터 팩이 부르는 REST 흉내 (MCP-SYS-01) — Bearer 토큰 필수
  GET /health · GET /calls                   헬스 · 최근 호출 기록(시험이 자격증명 주입·egress 를 확인)
  GET /mcp-registry/v0/servers               공개 MCP Registry API 흉내 (X-MKT-06, cursor 페이지)
  /agent/<name>/<ok|fail>/<version>/…       가짜 A2A 에이전트 (readyz·Agent Card·/a2a — fail 이면 /a2a 500) — 카나리·드리프트 시험 (PLT-DEP-05·09)
  POST /siem/services/collector/event        Splunk HEC 흉내 (X-RPT-05, "Splunk <token>") · GET /siem/received · POST /admin/siem-down
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKENS = {k: os.environ.get(f"FAKE_{k.upper()}_TOKEN", f"fake-{k}-token") for k in ("github", "slack", "jira")}
SIEM_TOKEN = os.environ.get("FAKE_SIEM_TOKEN", "fake-siem-token")
MCP_PUBLIC = [
    {"server": {"name": "io.github.example/weather", "version": "2.1.0", "description": "공개 날씨 MCP 서버",
                "remotes": [{"type": "streamable-http", "url": "http://mcp-demo:8000/mcp"}]},
     "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "publishedAt": "2026-09-01T00:00:00Z"}}},
    {"server": {"name": "io.github.example/notes", "version": "0.3.0", "description": "공개 메모 MCP 서버",
                "remotes": [{"type": "streamable-http", "url": "http://mcp-demo:8000/mcp"}]},
     "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "publishedAt": "2026-09-15T00:00:00Z"}}},
]
DOCS = {
    "faq": "<html><head><title>자주 묻는 질문</title></head><body><h1>자주 묻는 질문</h1><p>교환은 수령 후 14일 이내 가능하다.</p>"
           "<script>alert(1)</script><p>교환 배송비는 고객 부담이다.</p></body></html>",
    "notice": "<html><body><h1>공지</h1><p>설 연휴 동안 고객센터는 쉰다.</p></body></html>",
}
CALLS: list[dict] = []
STATE = {"issues": [{"number": 1, "title": "로그인 오류", "state": "open"}], "messages": [], "tickets": [{"key": "ATL-1", "summary": "배포 승인", "status": "To Do"}],
         "down": False, "siem_down": False, "siem": []}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def auth(self, system):
        return self.headers.get("Authorization", "") == f"Bearer {TOKENS[system]}"

    def handle_any(self, method):
        path = self.path.split("?")[0]
        body, raw = {}, b""
        if method == "POST":
            n = int(self.headers.get("content-length", 0))
            raw = self.rfile.read(n)
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
        if path == "/health":
            return self.send(200, {"status": "ok"})
        if path == "/calls":
            return self.send(200, {"items": CALLS[-200:]})
        if path == "/admin/down":                # 헬스·SLA 시험: 장애 흉내 on/off
            STATE["down"] = bool(body.get("down"))
            return self.send(200, {"down": STATE["down"]})
        if path.startswith("/agent/"):           # /agent/<name>/<mode>/<version>/<rest>
            parts = path.split("/")
            if len(parts) >= 6:
                name, mode, ver = parts[2], parts[3], parts[4]
                tail = parts[5:]
                tags = []                                     # 선택 꼬리표 (같은 에이전트의 다른 엔드포인트 — 블루그린·게이트 시험)
                while tail and tail[0] not in ("readyz", ".well-known", "a2a"):
                    tags.append(tail.pop(0))
                rest = "/" + "/".join(tail)
                base = f"http://{self.headers.get('Host', 'fake-upstream:8000')}/agent/{name}/{mode}/{ver}" + "".join("/" + t for t in tags)
                if rest == "/readyz":
                    return self.send(200, {"status": "ok"})
                if rest == "/.well-known/agent-card.json":
                    return self.send(200, {"name": name, "description": f"fake {mode} agent", "version": ver, "capabilities": {},
                                           "defaultInputModes": ["text/plain"], "defaultOutputModes": ["application/json"],
                                           "skills": [{"id": "loop.plan", "name": "plan", "description": "fake", "tags": []}],
                                           "supportedInterfaces": [{"url": base + "/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}]})
                if rest == "/a2a":
                    if mode == "fail":
                        return self.send(500, {"error": "canary broken"})
                    return self.send(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {"fake": name, "version": ver}})
            return self.send(404, {"error": "no route"})
        if path == "/mcp-registry/v0/servers":   # 공식 Registry API v0 형식 · 1개씩 cursor 페이지
            q = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&") if "=" in p) if "?" in self.path else {}
            i = int(q.get("cursor", "0") or 0)
            nxt = str(i + 1) if i + 1 < len(MCP_PUBLIC) else None
            return self.send(200, {"servers": MCP_PUBLIC[i:i + 1], "metadata": {"count": 1, **({"nextCursor": nxt} if nxt else {})}})
        if path == "/admin/siem-down":
            STATE["siem_down"] = bool(body.get("down"))
            return self.send(200, {"down": STATE["siem_down"]})
        if path == "/siem/received":
            return self.send(200, {"count": len(STATE["siem"]), "items": STATE["siem"][-50:]})
        if path == "/siem/services/collector/event":
            if self.headers.get("Authorization", "") != f"Splunk {SIEM_TOKEN}":
                return self.send(401, {"text": "Invalid token", "code": 4})
            if STATE["siem_down"]:
                return self.send(503, {"text": "Server is busy", "code": 9})
            evs = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
            STATE["siem"].extend(evs)
            return self.send(200, {"text": "Success", "code": 0, "ackId": len(STATE["siem"])})
        if path.startswith("/docs/"):
            name = path.rsplit("/", 1)[1].removesuffix(".html")
            return self.send(200, DOCS[name], "text/html; charset=utf-8") if name in DOCS else self.send(404, {"error": "no doc"})
        system = path.strip("/").split("/")[0]
        if system not in TOKENS:
            return self.send(404, {"error": "unknown"})
        CALLS.append({"ts": time.time(), "system": system, "method": method, "path": path, "auth": bool(self.headers.get("Authorization"))})
        if STATE["down"]:
            return self.send(503, {"error": "upstream down"})
        if not self.auth(system):
            return self.send(401, {"error": "bad token"})
        if path == "/github/repos/atlas/app/issues":
            if method == "POST":
                it = {"number": len(STATE["issues"]) + 1, "title": body.get("title", ""), "state": "open"}
                STATE["issues"].append(it)
                return self.send(201, it)
            return self.send(200, STATE["issues"])
        if path == "/slack/chat.postMessage" and method == "POST":
            STATE["messages"].append(body)
            return self.send(200, {"ok": True, "ts": str(time.time())})
        if path == "/jira/rest/api/3/search":
            return self.send(200, {"issues": STATE["tickets"]})
        return self.send(404, {"error": "no route"})

    def do_GET(self):  # noqa: N802
        self.handle_any("GET")

    def do_POST(self):  # noqa: N802
        self.handle_any("POST")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
