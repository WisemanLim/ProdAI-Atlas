"""시험 하네스 전용 — 호스트 8080 을 다른 프로젝트가 쓰고 있을 때 Manager 를 다른 포트(MANAGER_PORT)로 띄워 기존 시험을 그대로 돌린다.
ATLAS_MANAGER_HOST_URL 이 설정돼 있을 때만 `http://127.0.0.1:8080` → 그 URL 로 urllib·httpx·subprocess 인자를 바꾼다.
ATLAS_AUTHSHIM=1 이면 impl 14th authshim(게이트 API 키 부착)도 함께 켠다. PYTHONPATH 로 명시적으로 켤 때만 동작(제품 코드 아님)."""
import os
import runpy
import subprocess
import urllib.request

_SRC = "http://127.0.0.1:8080"
_DST = os.environ.get("ATLAS_MANAGER_HOST_URL", "").rstrip("/")
_ON = bool(_DST) and _DST != _SRC


def rw(s):
    return s.replace(_SRC, _DST) if _ON and isinstance(s, str) else s


if _ON:
    _orig = urllib.request.urlopen

    def _urlopen(req, *a, **kw):
        if isinstance(req, urllib.request.Request):
            req.full_url = rw(req.full_url)
        else:
            req = rw(str(req))
        return _orig(req, *a, **kw)
    urllib.request.urlopen = _urlopen

    _popen_init = subprocess.Popen.__init__

    def _popen(self, args, *a, **kw):
        args = [rw(x) for x in args] if isinstance(args, (list, tuple)) else rw(args)
        _popen_init(self, args, *a, **kw)
    subprocess.Popen.__init__ = _popen

    try:
        import httpx
        _req_init = httpx.Request.__init__

        def _request(self, method, url, *a, **kw):
            _req_init(self, method, rw(str(url)) if str(url).startswith(_SRC) else url, *a, **kw)
        httpx.Request.__init__ = _request
    except ImportError:
        pass

if os.environ.get("ATLAS_AUTHSHIM") == "1":   # 게이트 API 키 부착 (impl 14th) — 재작성된 호스트도 대상
    os.environ.setdefault("ATLAS_TEST_MANAGER", ",".join(x for x in ("http://127.0.0.1:8080", _DST, "http://127.0.0.1:5173") if x))
    runpy.run_path(os.path.join(os.path.dirname(__file__), "../../14th/authshim/sitecustomize.py"))
