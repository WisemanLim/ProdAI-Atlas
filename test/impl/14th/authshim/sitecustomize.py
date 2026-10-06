"""시험 하네스 전용 — ATLAS_AUTH_MODE=oidc 스택에서 기존 스모크(인증 이전 작성)를 그대로 돌리기 위해
Manager(:8080)·콘솔 프록시(:5173) 의 /api·/svc 요청에 ATLAS_TEST_TOKEN 을 붙인다. PYTHONPATH 로 명시적으로 켤 때만 동작(제품 코드 아님)."""
import os
import urllib.request

_TOKEN = os.environ.get("ATLAS_TEST_TOKEN", "")
_HOSTS = tuple(os.environ.get("ATLAS_TEST_MANAGER", "http://127.0.0.1:8080,http://127.0.0.1:5173").split(","))  # Manager · 콘솔 프록시
_orig = urllib.request.urlopen


def _urlopen(req, *a, **kw):
    url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    if _TOKEN and url.startswith(_HOSTS) and ("/api/" in url or "/svc/" in url):
        if not isinstance(req, urllib.request.Request):
            req = urllib.request.Request(req)
        if not req.has_header("Authorization"):
            req.add_header("Authorization", f"Bearer {_TOKEN}")
    return _orig(req, *a, **kw)


urllib.request.urlopen = _urlopen
