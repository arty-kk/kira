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
    def __init__(self, rows):
        self.rows = list(rows)
        self.execute_calls = 0
        self.executed_statements = []

    async def execute(self, stmt):
        self.execute_calls += 1
        self.executed_statements.append(stmt)
        value = self.rows.pop(0) if self.rows else None
        return _FakeResult(value)


class _UpdateChain:
    def __init__(self):
        self.where_args = None
        self.values_kwargs = None

    def where(self, *args, **_kwargs):
        self.where_args = args
        return self

    def values(self, **kwargs):
        self.values_kwargs = kwargs
        return self

    def returning(self, *_args, **_kwargs):
        return self


class _Func:
    @staticmethod
    def now():
        return "now"


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
        class _Column:
            def __eq__(self, _other):
                return self

            def is_(self, _other):
                return self

            def is_not(self, _other):
                return self

        id = _Column()
        notified_at = _Column()
        lease_token = _Column()
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
    async def test_notify_claim_happens_once_for_two_calls(self):
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
        db = _FakeDB([1, 1, None])
        update_calls = []

        @asynccontextmanager
        async def _fake_session_scope(*_args, **_kwargs):
            yield db

        def _fake_update(*_args, **_kwargs):
            stmt = _UpdateChain()
            update_calls.append(stmt)
            return stmt

        send_mock = AsyncMock(return_value=object())

        with (
            patch.object(payments, "send_message_safe", send_mock),
            patch.object(payments, "session_scope", _fake_session_scope),
            patch.object(payments, "t", AsyncMock(return_value="sent")),
            patch.object(payments, "update", _fake_update),
            patch.object(payments, "func", _Func()),
            patch.object(payments.uuid, "uuid4", return_value=SimpleNamespace(hex="notify-token")),
        ):
            await payments._notify_payment_result(outbox, remaining=10, duplicate=False)
            outbox.notified_at = None
            await payments._notify_payment_result(outbox, remaining=10, duplicate=False)

        send_mock.assert_awaited_once()
        self.assertEqual(len(update_calls), 3)
        self.assertEqual(update_calls[0].values_kwargs, {"notified_at": "now", "lease_token": "notify-token"})
        self.assertEqual(update_calls[1].values_kwargs, {"lease_token": None})
        self.assertEqual(update_calls[2].values_kwargs, {"notified_at": "now", "lease_token": "notify-token"})

    async def test_notify_rolls_back_claim_when_send_fails(self):
        payments = _load_payments_module()
        outbox = SimpleNamespace(
            id=2,
            user_id=99,
            kind="buy",
            requests_amount=5,
            gift_title=None,
            gift_emoji=None,
            gift_code=None,
            stars_amount=10,
            telegram_payment_charge_id="charge_fail",
            notified_at=None,
        )
        db = _FakeDB([1, 1])
        update_calls = []

        @asynccontextmanager
        async def _fake_session_scope(*_args, **_kwargs):
            yield db

        def _fake_update(*_args, **_kwargs):
            stmt = _UpdateChain()
            update_calls.append(stmt)
            return stmt

        with (
            patch.object(payments, "session_scope", _fake_session_scope),
            patch.object(payments, "send_message_safe", AsyncMock(return_value=None)),
            patch.object(payments, "t", AsyncMock(return_value="sent")),
            patch.object(payments, "update", _fake_update),
            patch.object(payments, "func", _Func()),
            patch.object(payments.uuid, "uuid4", return_value=SimpleNamespace(hex="notify-token")),
        ):
            await payments._notify_payment_result(outbox, remaining=1, duplicate=False)

        self.assertIsNone(outbox.notified_at)
        self.assertEqual(db.execute_calls, 2)
        self.assertEqual(update_calls[0].values_kwargs, {"notified_at": "now", "lease_token": "notify-token"})
        self.assertEqual(update_calls[1].values_kwargs, {"notified_at": None, "lease_token": None})

    async def test_gift_side_effects_run_after_successful_send_only(self):
        payments = _load_payments_module()
        outbox = SimpleNamespace(
            id=3,
            user_id=11,
            kind="gift",
            requests_amount=7,
            gift_title="Rose",
            gift_emoji="🌹",
            gift_code="gift-1",
            stars_amount=200,
            telegram_payment_charge_id="charge_gift",
            notified_at=None,
        )

        db = _FakeDB([1, 1])
        update_calls = []

        @asynccontextmanager
        async def _fake_session_scope(*_args, **_kwargs):
            yield db

        send_mock = AsyncMock(return_value=object())
        push_mock = AsyncMock(return_value=None)

        def _fake_update(*_args, **_kwargs):
            stmt = _UpdateChain()
            update_calls.append(stmt)
            return stmt

        with (
            patch.object(payments, "session_scope", _fake_session_scope),
            patch.object(payments, "send_message_safe", send_mock),
            patch.object(payments, "push_message", push_mock),
            patch.object(payments, "t", AsyncMock(return_value="gift sent")),
            patch.object(payments, "update", _fake_update),
            patch.object(payments, "func", _Func()),
            patch.object(payments.uuid, "uuid4", return_value=SimpleNamespace(hex="notify-token")),
            patch.object(payments.celery, "send_task") as send_task_mock,
        ):
            await payments._notify_payment_result(outbox, remaining=30, duplicate=False)

        send_mock.assert_awaited_once()
        push_mock.assert_awaited_once()
        send_task_mock.assert_called_once()
        self.assertEqual(update_calls[1].values_kwargs, {"lease_token": None})


class _FakeRequeueNotifyResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def all(self):
        return self._value


class _FakeRequeueNotifyDB:
    def __init__(self, charge_id):
        self.charge_id = charge_id
        self.claimed_once = False

    async def execute(self, stmt, params=None):
        stmt_text = str(stmt)
        if params is not None and "RETURNING telegram_payment_charge_id, lease_token" in stmt_text:
            if self.claimed_once:
                return _FakeRequeueNotifyResult([])
            self.claimed_once = True
            return _FakeRequeueNotifyResult([(self.charge_id, params["lease_token"])])
        return _FakeRequeueNotifyResult(1)


class RequeueAppliedNotifyFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_notify_via_requeue_applied_unnotified_without_reapply(self):
        payments = _load_payments_module()
        outbox = SimpleNamespace(
            id=10,
            user_id=77,
            kind="buy",
            status="applied",
            requests_amount=4,
            gift_title=None,
            gift_emoji=None,
            gift_code=None,
            stars_amount=100,
            telegram_payment_charge_id="charge_retry_notify",
            notified_at=None,
        )

        db = _FakeRequeueNotifyDB(charge_id=outbox.telegram_payment_charge_id)

        @asynccontextmanager
        async def _fake_session_scope(*_args, **_kwargs):
            yield db

        send_mock = AsyncMock(side_effect=[None, object()])

        with (
            patch.object(payments, "session_scope", _fake_session_scope),
            patch.object(payments, "send_message_safe", send_mock),
            patch.object(payments, "t", AsyncMock(return_value="sent")),
            patch.object(payments, "update", lambda *_args, **_kwargs: _UpdateChain()),
            patch.object(payments, "func", _Func()),
            patch.object(payments.uuid, "uuid4", side_effect=[SimpleNamespace(hex="notify-1"), SimpleNamespace(hex="lease-1"), SimpleNamespace(hex="notify-2")]),
            patch.object(payments.celery, "send_task") as send_task_mock,
        ):
            await payments._notify_payment_result(outbox, remaining=15, duplicate=True)
            self.assertIsNone(outbox.notified_at)

            enqueued, errors = await payments.requeue_applied_unnotified_outbox(batch_size=10)
            self.assertEqual((enqueued, errors), (1, 0))
            send_task_mock.assert_called_once_with("payments.process_outbox", args=["charge_retry_notify"])

            await payments._notify_payment_result(outbox, remaining=15, duplicate=True)

        self.assertIsNotNone(outbox.notified_at)
        self.assertEqual(send_mock.await_count, 2)
        payments.add_paid_requests.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
