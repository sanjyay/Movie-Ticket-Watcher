import pytest
from pydantic import ValidationError

from app.config import Settings


def test_container_requires_session_secret() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(_env_file=None, container_deployment=True, secret_key="short")


def test_invalid_telegram_url_fails_clearly() -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_API_BASE"):
        Settings(_env_file=None, telegram_api_base="http://insecure.example")
