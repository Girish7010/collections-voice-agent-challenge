import pytest

from app.config import Settings


def test_defaults():
    s = Settings()
    assert s.dependency_url == "http://localhost:8001"
    assert s.max_attempts == 3


def test_from_env(monkeypatch):
    monkeypatch.setenv("APP_DEPENDENCY_URL", "http://mock:8001")
    monkeypatch.setenv("APP_DB_PATH", "/tmp/x.db")
    monkeypatch.setenv("APP_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("APP_ATTEMPT_TIMEOUT_S", "1.5")
    monkeypatch.setenv("APP_CB_OPEN_S", "3")

    s = Settings.from_env()

    assert (s.dependency_url, s.db_path, s.max_attempts, s.attempt_timeout_s, s.cb_open_s) == \
        ("http://mock:8001", "/tmp/x.db", 5, 1.5, 3.0)


def test_bad_env_value_gives_clear_error(monkeypatch):
    monkeypatch.setenv("APP_MAX_ATTEMPTS", "lots")
    with pytest.raises(ValueError, match="APP_MAX_ATTEMPTS"):
        Settings.from_env()
