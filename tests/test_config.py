import pytest
from pydantic import ValidationError

from app.config import Settings


def test_container_requires_session_secret() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(_env_file=None, container_deployment=True, secret_key="short")


def test_invalid_ntfy_url_fails_clearly() -> None:
    with pytest.raises(ValidationError, match="NTFY_SERVER"):
        Settings(_env_file=None, ntfy_server="http://insecure.example")
