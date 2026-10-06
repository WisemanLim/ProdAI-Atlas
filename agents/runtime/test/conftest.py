"""시험 공통 — 앱 import 전에 임시 SQLite 로 DATABASE_URL 을 바꾸고 Manager 자가 등록·Gateway·OTLP 내보내기를 끈다."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="atlas-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["DB_ENGINE"] = "sqlite"
os.environ["ATLAS_MANAGER_URL"] = ""
os.environ.pop("ATLAS_GATEWAY_URL", None)
os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)  # 시험은 수집기로 내보내지 않음 (in-memory exporter 사용)

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    from app.main import app

    with TestClient(app) as c:  # lifespan → alembic upgrade head
        yield c


@pytest.fixture
def rpc(client):
    def call(method, params=None, rid=1):
        res = client.post("/a2a", json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        assert res.status_code == 200
        return res.json()

    return call


def send(rpc, text, **params):
    msg = {"role": "ROLE_USER", "parts": [{"text": text}], **params.pop("message", {})}
    return rpc("SendMessage", {"message": msg, **params})
