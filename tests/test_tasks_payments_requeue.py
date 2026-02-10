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

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def with_for_update(self, **_kwargs):
        return self


class _ScalarsResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _FakeResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return _ScalarsResult(self._values)


class _FakeDB:
    def __init__(self, charge_ids):
        self._charge_ids = charge_ids

    async def execute(self, _stmt):
        return _FakeResult(self._charge_ids)


class _FakeColumn:
    def __eq__(self, _other):
        return self

    def is_(self, _other):
        return self


class _FakePaymentOutbox:
    telegram_payment_charge_id = _FakeColumn()
    status = _FakeColumn()
    applied_at = _FakeColumn()
    id = _FakeColumn()


def _load_payments_module():
    module_name = "tasks_payments_requeue_under_test"
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
    target_modules["app.core.models"].GiftPurchase = _FakePaymentOutbox
    target_modules["app.core.models"].PaymentOutbox = _FakePaymentOutbox
    target_modules["app.core.models"].PaymentReceipt = _FakePaymentOutbox
    target_modules["app.core.models"].User = _FakePaymentOutbox
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


class RequeuePendingOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_requeue_pending_outbox_enqueues_process_task_for_pending_rows(self):
        payments = _load_payments_module()
        fake_db = _FakeDB(["charge_1", "charge_2"])

        @asynccontextmanager
        async def _fake_session_scope(*_args, **_kwargs):
            yield fake_db

        with (
            patch.object(payments, "session_scope", _fake_session_scope),
            patch.object(payments, "select", lambda *_args, **_kwargs: _SelectChain()),
            patch.object(payments.celery, "send_task") as send_task_mock,
        ):
            enqueued, enqueue_errors = await payments.requeue_pending_outbox(batch_size=20)

        self.assertEqual(enqueued, 2)
        self.assertEqual(enqueue_errors, 0)
        self.assertEqual(send_task_mock.call_count, 2)
        send_task_mock.assert_any_call("payments.process_outbox", args=["charge_1"])
        send_task_mock.assert_any_call("payments.process_outbox", args=["charge_2"])


if __name__ == "__main__":
    unittest.main()
