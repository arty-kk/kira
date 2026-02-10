import importlib.util
import pathlib
import sys
import types
import unittest
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

for name in list(sys.modules):
    if name == "app" or name.startswith("app."):
        sys.modules.pop(name, None)

fake_tasks = types.ModuleType("app.tasks")
fake_celery_app = types.ModuleType("app.tasks.celery_app")
fake_celery_app.celery = SimpleNamespace(send_task=lambda *_args, **_kwargs: None)
fake_celery_app._run = lambda _coro: None
sys.modules["app.tasks"] = fake_tasks
sys.modules["app.tasks.celery_app"] = fake_celery_app

payments_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "bot" / "handlers" / "payments.py"
spec = importlib.util.spec_from_file_location("payments_under_test", payments_path)
payments = importlib.util.module_from_spec(spec)
sys.modules["payments_under_test"] = payments
spec.loader.exec_module(payments)


class _FakeInsert:
    def values(self, **_kwargs):
        return self

    def on_conflict_do_nothing(self, **_kwargs):
        return self

    def returning(self, *_args, **_kwargs):
        return self


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self):
        self._first = True
        self.row = SimpleNamespace(status="pending", last_error=None)
        self.execute_calls = 0

    async def execute(self, _stmt):
        self.execute_calls += 1
        if self._first:
            self._first = False
            return _FakeResult("pending")
        return _FakeResult(self.row)


class PaymentOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_payment_success_enqueues_outbox_task(self) -> None:
        fake_db = _FakeDB()

        @asynccontextmanager
        async def _fake_session_scope(*_args, **_kwargs):
            yield fake_db

        dummy_payment = SimpleNamespace(
            invoice_payload="buy_1",
            telegram_payment_charge_id="charge_123",
            provider_payment_charge_id="prov_1",
            currency=payments.settings.PAYMENT_CURRENCY,
            total_amount=1,
        )
        dummy_message = SimpleNamespace(
            successful_payment=dummy_payment,
            from_user=SimpleNamespace(id=1, full_name="Test User"),
            chat=SimpleNamespace(id=1),
            message_id=10,
        )

        with (
            patch.object(payments, "session_scope", _fake_session_scope),
            patch.object(payments, "pg_insert", lambda _model: _FakeInsert()),
            patch.object(payments, "get_or_create_user", AsyncMock(return_value=SimpleNamespace(id=1))),
            patch.object(payments, "clear_payment_ui", AsyncMock()),
            patch.object(payments, "clear_payment_runtime_keys", AsyncMock()),
            patch.object(payments, "send_transient_notice", AsyncMock()),
            patch.object(payments, "tr", AsyncMock(side_effect=lambda *_args, **_kwargs: _kwargs.get("default", ""))),
            patch.object(payments, "purchase_tiers", lambda: {1: 1}),
            patch.object(payments.celery, "send_task") as send_task,
        ):
            await payments.on_payment_success(dummy_message)

        send_task.assert_called_with("payments.process_outbox", args=["charge_123"])

    async def test_payment_success_marks_outbox_failed_when_enqueue_fails(self) -> None:
        fake_db = _FakeDB()

        @asynccontextmanager
        async def _fake_session_scope(*_args, **_kwargs):
            yield fake_db

        dummy_payment = SimpleNamespace(
            invoice_payload="buy_1",
            telegram_payment_charge_id="charge_456",
            provider_payment_charge_id="prov_2",
            currency=payments.settings.PAYMENT_CURRENCY,
            total_amount=1,
        )
        dummy_message = SimpleNamespace(
            successful_payment=dummy_payment,
            from_user=SimpleNamespace(id=2, full_name="Test User"),
            chat=SimpleNamespace(id=2),
            message_id=11,
        )

        async def _fake_tr(_uid, key, default="", **_kwargs):
            if key == "payments.error":
                return "⚠️ Temporary payment processing error."
            if key == "payments.processing":
                return "✅ Processing payment now."
            return default

        with (
            patch.object(payments, "session_scope", _fake_session_scope),
            patch.object(payments, "pg_insert", lambda _model: _FakeInsert()),
            patch.object(payments, "get_or_create_user", AsyncMock(return_value=SimpleNamespace(id=1))),
            patch.object(payments, "clear_payment_ui", AsyncMock()),
            patch.object(payments, "clear_payment_runtime_keys", AsyncMock()),
            patch.object(payments, "send_transient_notice", AsyncMock()) as send_notice,
            patch.object(payments, "tr", AsyncMock(side_effect=_fake_tr)),
            patch.object(payments, "purchase_tiers", lambda: {1: 1}),
            patch.object(payments.celery, "send_task", side_effect=Exception("broker down")),
        ):
            await payments.on_payment_success(dummy_message)

        send_notice.assert_called_once()
        sent_text = send_notice.call_args.args[1]
        self.assertEqual(sent_text, "⚠️ Temporary payment processing error.")
        self.assertNotEqual(sent_text, "✅ Processing payment now.")
        self.assertEqual(fake_db.row.status, "failed")
        self.assertEqual(fake_db.row.last_error, "broker down")
        self.assertEqual(fake_db.execute_calls, 2)


if __name__ == "__main__":
    unittest.main()
