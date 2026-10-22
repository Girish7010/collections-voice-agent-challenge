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


def test_unknown_store_backend_is_rejected():
    with pytest.raises(ValueError, match="APP_STORE_BACKEND"):
        Settings(store_backend="redis")


def test_dynamodb_backend_requires_table_names():
    with pytest.raises(ValueError, match="APP_DLQ_TABLE and APP_LEDGER_TABLE"):
        Settings(store_backend="dynamodb")
    with pytest.raises(ValueError, match="APP_LEDGER_TABLE"):
        Settings(store_backend="dynamodb", dlq_table="d")

    s = Settings(store_backend="dynamodb", dlq_table="d", ledger_table="l")
    assert (s.dlq_table, s.ledger_table) == ("d", "l")


def test_sqlite_refuses_a_wider_store_pool():
    """Concurrent schema creation and the WAL pragma deadlock, so SQLite stays at one."""
    with pytest.raises(ValueError, match="APP_DB_THREADS=1"):
        Settings(store_backend="sqlite", db_threads=4)


def test_store_settings_come_from_env(monkeypatch):
    monkeypatch.setenv("APP_STORE_BACKEND", "dynamodb")
    monkeypatch.setenv("APP_DLQ_TABLE", "dl")
    monkeypatch.setenv("APP_LEDGER_TABLE", "lg")
    monkeypatch.setenv("APP_AWS_REGION", "ap-south-1")
    monkeypatch.setenv("APP_LEDGER_TTL_DAYS", "14")
    monkeypatch.setenv("APP_DB_THREADS", "8")

    s = Settings.from_env()

    assert (s.store_backend, s.dlq_table, s.ledger_table, s.aws_region,
            s.ledger_ttl_days, s.db_threads) == ("dynamodb", "dl", "lg", "ap-south-1", 14, 8)
