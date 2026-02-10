import importlib.util
import pathlib
import sys
import types
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class _FakeColumn:
    def __eq__(self, _other):
        return self


class _FakePaymentOutbox:
    status = _FakeColumn()
    telegram_payment_charge_id = _FakeColumn()


class _FakeDispatcher:
    def message(self, *_args, **_kwargs):
        def _decorator(func):
            return func

        return _decorator

    def callback_query(self, *_args, **_kwargs):
        def _decorator(func):
            return func

        return _decorator

    def pre_checkout_query(self, *_args, **_kwargs):
        def _decorator(func):
            return func

        return _decorator


def _load_payments_module():
    module_name = "payments_handler_under_test"
    target_modules = {
        "app": types.ModuleType("app"),
        "app.bot": types.ModuleType("app.bot"),
        "app.bot.components": types.ModuleType("app.bot.components"),
        "app.bot.components.constants": types.ModuleType("app.bot.components.constants"),
        "app.bot.components.dispatcher": types.ModuleType("app.bot.components.dispatcher"),
        "app.bot.i18n": types.ModuleType("app.bot.i18n"),
        "app.bot.utils": types.ModuleType("app.bot.utils"),
        "app.bot.utils.shop_tiers": types.ModuleType("app.bot.utils.shop_tiers"),
        "app.bot.utils.telegram_safe": types.ModuleType("app.bot.utils.telegram_safe"),
        "app.clients": types.ModuleType("app.clients"),
        "app.clients.telegram_client": types.ModuleType("app.clients.telegram_client"),
        "app.config": types.ModuleType("app.config"),
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

    target_modules["app.bot.components.constants"].redis_client = SimpleNamespace(
        exists=AsyncMock(return_value=False),
        set=AsyncMock(return_value=True),
        get=AsyncMock(return_value=None),
        delete=AsyncMock(return_value=1),
    )
    target_modules["app.bot.components.dispatcher"].dp = _FakeDispatcher()
    target_modules["app.bot.i18n"].t = AsyncMock(return_value="")
    target_modules["app.bot.utils.shop_tiers"].find_gift = lambda *_args, **_kwargs: None
    target_modules["app.bot.utils.shop_tiers"].gift_display_name = lambda *_args, **_kwargs: "gift"
    target_modules["app.bot.utils.shop_tiers"].gift_tiers = lambda *_args, **_kwargs: []
    target_modules["app.bot.utils.shop_tiers"].purchase_tiers = lambda *_args, **_kwargs: {1: 1}
    target_modules["app.bot.utils.telegram_safe"].delete_message_safe = AsyncMock()
    target_modules["app.bot.utils.telegram_safe"].send_invoice_safe = AsyncMock()
    target_modules["app.bot.utils.telegram_safe"].send_message_safe = AsyncMock()
    target_modules["app.clients.telegram_client"].get_bot = lambda: SimpleNamespace()
    target_modules["app.config"].settings = SimpleNamespace(PAYMENT_CURRENCY="XTR")

    @asynccontextmanager
    async def _dummy_session_scope(*_args, **_kwargs):
        yield None

    target_modules["app.core.db"].session_scope = _dummy_session_scope
    target_modules["app.core.memory"].push_message = AsyncMock()
    target_modules["app.core.models"].PaymentOutbox = _FakePaymentOutbox
    target_modules["app.services.user.user_service"].compute_remaining = lambda _user: 0
    target_modules["app.services.user.user_service"].get_or_create_user = AsyncMock(return_value=SimpleNamespace(id=1))
    target_modules["app.tasks.celery_app"].celery = SimpleNamespace(send_task=lambda *_args, **_kwargs: None)

    previous = {}
    names = set(target_modules) | {module_name}
    for name in names:
        previous[name] = sys.modules.get(name)
        sys.modules.pop(name, None)

    try:
        sys.modules.update(target_modules)
        payments_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "bot" / "handlers" / "payments.py"
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


class _SelectChain:
    def where(self, *_args, **_kwargs):
        return self

    def with_for_update(self):
        return self


class PaymentOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_payment_success_enqueues_outbox_task(self) -> None:
        payments = _load_payments_module()
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
            patch.object(payments, "select", lambda *_args, **_kwargs: _SelectChain()),
            patch.object(payments, "send_transient_notice", AsyncMock()),
            patch.object(payments, "tr", AsyncMock(side_effect=lambda *_args, **_kwargs: _kwargs.get("default", ""))),
            patch.object(payments, "purchase_tiers", lambda: {1: 1}),
            patch.object(payments.celery, "send_task") as send_task,
        ):
            await payments.on_payment_success(dummy_message)

        send_task.assert_called_with("payments.process_outbox", args=["charge_123"])

    async def test_payment_success_keeps_outbox_pending_when_enqueue_fails(self) -> None:
        payments = _load_payments_module()
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
            patch.object(payments, "select", lambda *_args, **_kwargs: _SelectChain()),
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
        self.assertEqual(fake_db.row.status, "pending")
        self.assertEqual(fake_db.row.last_error, "broker down")
        self.assertEqual(fake_db.execute_calls, 2)


if __name__ == "__main__":
    unittest.main()
