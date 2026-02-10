import importlib.util
import pathlib
import sys
import types
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class _SelectChain:
    def where(self, *_args, **_kwargs):
        return self

    def with_for_update(self):
        return self


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, row):
        self.row = row

    async def execute(self, _stmt):
        return _FakeResult(self.row)


def _load_payments_module():
    module_name = "tasks_payments_under_test"
    target_modules = {
        "app": types.ModuleType("app"),
        "app.bot": types.ModuleType("app.bot"),
        "app.bot.utils": types.ModuleType("app.bot.utils"),
        "app.bot.utils.telegram_safe": types.ModuleType("app.bot.utils.telegram_safe"),
        "app.bot.i18n": types.ModuleType("app.bot.i18n"),
        "app.clients": types.ModuleType("app.clients"),
        "app.clients.telegram_client": types.ModuleType("app.clients.telegram_client"),
        "app.core": types.ModuleType("app.core"),
        "app.core.db": types.ModuleType("app.core.db"),
        "app.core.memory": types.ModuleType("app.core.memory"),
        "app.core.models": types.ModuleType("app.core.models"),
        "app.services": types.ModuleType("app.services"),
        "app.services.user": types.ModuleType("app.services.user"),
        "app.services.user.user_service": types.ModuleType("app.services.user.user_service"),
        "app.tasks": types.ModuleType("app.tasks"),
        "app.tasks.celery_app": types.ModuleType("app.tasks.celery_app"),
    }

    target_modules["app.bot.utils.telegram_safe"].send_message_safe = AsyncMock()
    target_modules["app.bot.i18n"].t = AsyncMock(return_value="")
    target_modules["app.clients.telegram_client"].get_bot = lambda: object()

    @asynccontextmanager
    async def _dummy_session_scope(*_args, **_kwargs):
        yield None

    target_modules["app.core.db"].session_scope = _dummy_session_scope
    target_modules["app.core.memory"].push_message = AsyncMock()

    class _Model:
        id = "id"
        telegram_payment_charge_id = "telegram_payment_charge_id"

    target_modules["app.core.models"].GiftPurchase = _Model
    target_modules["app.core.models"].PaymentOutbox = _Model
    target_modules["app.core.models"].PaymentReceipt = _Model
    target_modules["app.core.models"].User = _Model
    target_modules["app.services.user.user_service"].add_paid_requests = AsyncMock()
    target_modules["app.services.user.user_service"].compute_remaining = lambda _user: 0

    class _Celery:
        def task(self, *args, **kwargs):
            def _decorator(func):
                return func

            return _decorator

        def send_task(self, *_args, **_kwargs):
            return None

    target_modules["app.tasks.celery_app"].celery = _Celery()
    target_modules["app.tasks.celery_app"]._run = lambda _coro: None

    previous = {}
    names = set(target_modules) | {module_name}
    for name in names:
        previous[name] = sys.modules.get(name)
        sys.modules.pop(name, None)

    try:
        sys.modules.update(target_modules)
        payments_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "tasks" / "payments.py"
        spec = importlib.util.spec_from_file_location(module_name, payments_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name in names:
            sys.modules.pop(name, None)
            if previous[name] is not None:
                sys.modules[name] = previous[name]


class NotifyPaymentResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_notify_sets_notified_at_when_message_is_sent(self):
        payments = _load_payments_module()
        outbox = SimpleNamespace(
            id=1,
            user_id=42,
            kind="buy",
            requests_amount=3,
            gift_title=None,
            gift_emoji=None,
            gift_code=None,
            stars_amount=50,
            telegram_payment_charge_id="charge_ok",
            notified_at=None,
        )
        row = SimpleNamespace(notified_at=None)
        fake_db = _FakeDB(row)

        @asynccontextmanager
        async def _fake_session_scope(*_args, **_kwargs):
            yield fake_db

        with (
            patch.object(payments, "send_message_safe", AsyncMock(return_value=object())),
            patch.object(payments, "session_scope", _fake_session_scope),
            patch.object(payments, "select", lambda *_args, **_kwargs: _SelectChain()),
            patch.object(payments, "t", AsyncMock(return_value="sent")),
        ):
            await payments._notify_payment_result(outbox, remaining=10, duplicate=False)

        self.assertIsNotNone(row.notified_at)

    async def test_notify_skips_notified_at_and_logs_warning_when_message_not_sent(self):
        payments = _load_payments_module()
        outbox = SimpleNamespace(
            id=2,
            user_id=84,
            kind="buy",
            requests_amount=5,
            gift_title=None,
            gift_emoji=None,
            gift_code=None,
            stars_amount=100,
            telegram_payment_charge_id="charge_none",
            notified_at=None,
        )

        @asynccontextmanager
        async def _fake_session_scope(*_args, **_kwargs):
            raise AssertionError("session_scope should not be called when send_message_safe returns None")
            yield

        with (
            patch.object(payments, "send_message_safe", AsyncMock(return_value=None)),
            patch.object(payments, "session_scope", _fake_session_scope),
            patch.object(payments, "t", AsyncMock(return_value="sent")),
            patch.object(payments.logger, "warning") as warning_mock,
        ):
            await payments._notify_payment_result(outbox, remaining=20, duplicate=False)

        self.assertIsNone(outbox.notified_at)
        warning_mock.assert_called_once_with(
            "payment_outbox: notify skipped charge_id=%s user_id=%s",
            "charge_none",
            84,
        )


if __name__ == "__main__":
    unittest.main()
