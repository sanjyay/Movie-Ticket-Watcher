from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app, settings


def test_health_checks_database_and_directories(tmp_path: Path, monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)

    def test_db():  # type: ignore[no-untyped-def]
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_db] = test_db
    for name in ("data_dir", "config_dir", "screenshot_dir", "log_dir"):
        monkeypatch.setattr(settings, name, tmp_path / name)
    monkeypatch.setattr(settings, "telegram_bot_token", "123456789:TOP_SECRET_TOKEN_VALUE")
    monkeypatch.setattr(settings, "telegram_default_chat_id", "-100123456")
    try:
        response = TestClient(app).get("/health")
        assert response.status_code == 200
        assert response.json()["database"] == "ok"
        assert response.json()["directories"] == "writable"
        assert response.json()["telegram_configured"] is True
        assert "TOP_SECRET" not in response.text
        assert "-100123456" not in response.text
    finally:
        app.dependency_overrides.clear()
