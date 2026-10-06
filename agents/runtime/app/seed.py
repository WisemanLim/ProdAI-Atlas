"""atlas-agent-runtime — 시드 데이터 (make db-seed). 구현 단계에서 채운다."""
from app import db

if __name__ == "__main__":
    db.ping()
    print("seed: nothing to load yet")
