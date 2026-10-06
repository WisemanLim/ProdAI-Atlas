"""Atlas 분석 샌드박스 (FR-MA-ANL-01·05) — 격리 컨테이너 안에서 파이썬 코드를 자원 한도와 함께 실행한다.

POST /run {code, lang, files{name: text}, timeout_s, mem_mb, cpu_s, egress{proxy, policy, token}} (헤더 X-Sandbox-Token)
  → {exit_code, stdout, stderr, result, artifacts[], timed_out, cpu_exceeded, duration_ms}
- lang: python(기본) · javascript(node) · shell(sh) — X-SBX-01. out/ 아래 파일은 artifacts 로 돌려준다(최대 10개·각 256KiB)
- egress: 기본 송신 없음(내부 전용망). 정책이 있으면 egress 프록시(허용목록)만 HTTP(S)_PROXY 로 연다 — X-SBX-02
- 실행마다 빈 임시 디렉터리(tmpfs), 환경변수 비움, `python -I`(사용자 site·환경 무시)
- RLIMIT: CPU(timeout+1s) · 주소공간(mem_mb) · 파일 크기(100MiB) · 파일 수 64 · 프로세스 32 · 코어 덤프 0
- 출력 64KiB, result.json 1MiB 상한. 컨테이너는 내부 전용 네트워크(외부 차단)·read-only·cap_drop ALL·no-new-privileges
"""
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("SANDBOX_TOKEN", "")
MAX_TIMEOUT = int(os.environ.get("SANDBOX_MAX_TIMEOUT_S", "300"))
MAX_MEM = int(os.environ.get("SANDBOX_MAX_MEM_MB", "4096"))
SLOTS = threading.BoundedSemaphore(int(os.environ.get("SANDBOX_CONCURRENCY", "2")))
OUT_CAP, RESULT_CAP, FILE_CAP = 64 << 10, 1 << 20, 100 << 20


# 자식 프로세스 안에서 한도를 건 뒤 사용자 코드로 exec 한다 (preexec_fn 은 스레드 서버에서 안전하지 않다)
LIMITER = """
import os, resource, sys
t, m, f, lang = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
for k, v in ((resource.RLIMIT_CPU, t), (resource.RLIMIT_AS, m << 20), (resource.RLIMIT_FSIZE, f),
             (resource.RLIMIT_NOFILE, 64), (resource.RLIMIT_NPROC, 32), (resource.RLIMIT_CORE, 0)):
    resource.setrlimit(k, (v, v))
if lang == "javascript":
    os.execv("/usr/bin/node", ["node", "--max-old-space-size=%d" % max(32, m // 2), "main.js"])
if lang == "shell":
    os.execv("/bin/sh", ["sh", "main.sh"])
os.execv(sys.executable, [sys.executable, "-I", "main.py"])
"""
MAIN = {"python": "main.py", "javascript": "main.js", "shell": "main.sh"}
ART_MAX, ART_CAP = 10, 256 << 10


def run(req: dict) -> dict:
    timeout_s = max(1, min(int(req.get("timeout_s", 30)), MAX_TIMEOUT))
    mem_mb = max(64, min(int(req.get("mem_mb", 1024)), MAX_MEM))
    cpu_s = max(1, min(int(req.get("cpu_s", timeout_s + 1)), timeout_s + 1))
    lang = req.get("lang", "python")
    if lang not in MAIN:
        return {"error": f"지원하지 않는 언어 {lang}"}
    d = tempfile.mkdtemp(prefix="run-", dir=os.environ.get("SANDBOX_WORK", "/tmp"))
    try:
        for name, text in (req.get("files") or {}).items():
            if "/" in name or name.startswith("."):
                return {"error": f"잘못된 파일 이름 {name}"}
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(str(text))
        with open(os.path.join(d, MAIN[lang]), "w", encoding="utf-8") as f:
            f.write(str(req.get("code", "")))
        os.mkdir(os.path.join(d, "out"))
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": d, "MPLBACKEND": "Agg", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
        eg = req.get("egress") or {}
        if eg.get("proxy") and eg.get("policy") and eg.get("token"):   # 허용목록 송신만 — 프록시가 정책으로 판정
            pu = eg["proxy"].replace("://", f"://{eg['policy']}:{eg['token']}@", 1)
            env.update({"HTTP_PROXY": pu, "HTTPS_PROXY": pu, "http_proxy": pu, "https_proxy": pu})
        t0 = time.monotonic()
        timed_out = False
        p = subprocess.Popen([sys.executable, "-I", "-c", LIMITER, str(cpu_s), str(mem_mb), str(FILE_CAP), lang], cwd=d,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, env=env)
        try:
            out, err = p.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(p.pid, 9)
            out, err = p.communicate()
        result = None
        rp = os.path.join(d, "result.json")
        if os.path.exists(rp) and os.path.getsize(rp) <= RESULT_CAP:
            try:
                with open(rp, encoding="utf-8") as f:
                    result = json.load(f)
            except ValueError:
                result = None
        arts = []
        for name in sorted(os.listdir(os.path.join(d, "out")))[:ART_MAX]:
            fp = os.path.join(d, "out", name)
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    data = f.read(ART_CAP + 1)
                arts.append({"name": name, "size": os.path.getsize(fp), "truncated": len(data) > ART_CAP, "text": data[:ART_CAP].decode(errors="replace")})
        return {"exit_code": p.returncode, "stdout": out[:OUT_CAP].decode(errors="replace"), "stderr": err[:OUT_CAP].decode(errors="replace"),
                "result": result, "artifacts": arts, "timed_out": timed_out, "cpu_exceeded": p.returncode in (-24, -9) and not timed_out,
                "duration_ms": int((time.monotonic() - t0) * 1000), "limits": {"timeout_s": timeout_s, "mem_mb": mem_mb, "cpu_s": cpu_s}}
    finally:
        shutil.rmtree(d, ignore_errors=True)


class H(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict):
        b = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(200 if self.path == "/health" else 404, {"status": "ok"})

    def do_POST(self):
        if self.path != "/run":
            return self._send(404, {"error": "not_found"})
        if not TOKEN or not hmac.compare_digest(self.headers.get("X-Sandbox-Token", ""), TOKEN):
            return self._send(401, {"error": "unauthorized"})
        n = int(self.headers.get("Content-Length", 0))
        if n > 8 << 20:
            return self._send(413, {"error": "too_large"})
        try:
            req = json.loads(self.rfile.read(n))
        except ValueError:
            return self._send(400, {"error": "bad_json"})
        if not SLOTS.acquire(timeout=30):
            return self._send(429, {"error": "busy"})
        try:
            self._send(200, run(req))
        finally:
            SLOTS.release()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
