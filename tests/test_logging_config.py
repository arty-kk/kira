import logging

from app.core.logging_config import setup_logging


def _root_level_name() -> str:
    return logging.getLevelName(logging.getLogger().level)


def _sqlalchemy_level_name() -> str:
    return logging.getLevelName(logging.getLogger("sqlalchemy").level)


def _sqlalchemy_engine_level_name() -> str:
    return logging.getLevelName(logging.getLogger("sqlalchemy.engine").level)


def test_setup_logging_prefers_process_specific_level(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("PROCESS_SPECIFIC_LOG_LEVEL", "WARNING")

    setup_logging()

    assert _root_level_name() == "WARNING"


def test_setup_logging_falls_back_to_global_level(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    monkeypatch.delenv("PROCESS_SPECIFIC_LOG_LEVEL", raising=False)

    setup_logging()

    assert _root_level_name() == "ERROR"


def test_setup_logging_suppresses_sqlalchemy_by_default(monkeypatch):
    monkeypatch.delenv("SQLALCHEMY_LOG_LEVEL", raising=False)

    setup_logging()

    assert _sqlalchemy_level_name() == "CRITICAL"
    assert _sqlalchemy_engine_level_name() == "CRITICAL"


def test_setup_logging_uses_explicit_sqlalchemy_level(monkeypatch):
    monkeypatch.setenv("SQLALCHEMY_LOG_LEVEL", "ERROR")

    setup_logging()

    assert _sqlalchemy_level_name() == "ERROR"
    assert _sqlalchemy_engine_level_name() == "ERROR"


def test_setup_logging_disables_sqlalchemy_engine_propagation(monkeypatch):
    monkeypatch.setenv("SQLALCHEMY_LOG_LEVEL", "CRITICAL")

    setup_logging()

    assert logging.getLogger("sqlalchemy.engine").propagate is False
