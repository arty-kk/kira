import importlib.util
import pathlib
import sys
import types
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch


class _FakeColumn:
    def __eq__(self, other):
        return _FakeWhereValue(other)

    def is_(self, _other):
        return self

    def is_not(self, _other):
        return self

    def __ge__(self, _other):
        return self


class _FakePaymentOutbox:
    telegram_payment_charge_id = _FakeColumn()
    status = _FakeColumn()
    applied_at = _FakeColumn()
    id = _FakeColumn()
    leased_at = _FakeColumn()
    lease_token = _FakeColumn()
    notified_at = _FakeColumn()



class _FakeWhereValue:
    def __init__(self, value):
        self.right = type("_Right", (), {"value": value})()


class _FakeReleaseStmt:
    table = _FakePaymentOutbox

    def __init__(self):
        self._where_criteria = []
        self._values = {}

    def where(self, *criteria):
        self._where_criteria = criteria
        return self

    def values(self, **values):
        self._values = values
        return self


def _fake_update(_model):
    return _FakeReleaseStmt()


class _FakeClaimResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, state):
        self._state = state

    async def execute(self, stmt, params=None):
        stmt_text = str(stmt)
        if "RETURNING telegram_payment_charge_id, lease_token" in stmt_text:
            batch_size = int(params["batch_size"])
            lease_token = params["lease_token"]
            claimed = []
            for row in self._state:
                status_filter = "applied" if "status = 'applied'" in stmt_text else "pending"
                if row["status"] != status_filter:
                    continue
                if status_filter == "pending" and row.get("applied_at") is not None:
                    continue
                if status_filter == "applied" and row.get("notified_at") is not None:
                    continue
                if row["lease_token"] is not None:
                    continue
                row["lease_token"] = lease_token
                row["leased_at"] = "now"
                row["lease_attempts"] += 1
                claimed.append((row["charge_id"], row["lease_token"]))
                if len(claimed) >= batch_size:
                    break
            return _FakeClaimResult(claimed)

        if getattr(stmt, "table", None) is _FakePaymentOutbox:
            charge_id = str(stmt._where_criteria[0].right.value)
            lease_token = str(stmt._where_criteria[2].right.value)
            values = stmt._values
            for row in self._state:
                if row["charge_id"] == charge_id and row["lease_token"] == lease_token:
                    row["leased_at"] = values.get("leased_at")
                    row["lease_token"] = values.get("lease_token")
                    row["last_error"] = values.get("last_error")
            return _FakeClaimResult([])

        raise AssertionError(f"Unexpected statement: {stmt_text}")


class _FakeSessionFactory:
    def __init__(self, state):
        self._state = state

    @asynccontextmanager
    async def __call__(self, *_args, **_kwargs):
        yield _FakeDB(self._state)


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
    async def test_requeue_pending_outbox_does_not_enqueue_same_charge_twice_without_ttl_expiry(self):
        payments = _load_payments_module()
        state = [
            {"charge_id": "charge_1", "status": "pending", "lease_token": None, "leased_at": None, "lease_attempts": 0, "last_error": None}
        ]

        with (
            patch.object(payments, "session_scope", _FakeSessionFactory(state)),
            patch.object(payments.celery, "send_task") as send_task_mock,
            patch.object(payments, "update", _fake_update),
        ):
            first_enqueued, first_errors = await payments.requeue_pending_outbox(batch_size=20)
            second_enqueued, second_errors = await payments.requeue_pending_outbox(batch_size=20)

        self.assertEqual(first_enqueued, 1)
        self.assertEqual(first_errors, 0)
        self.assertEqual(second_enqueued, 0)
        self.assertEqual(second_errors, 0)
        self.assertEqual(send_task_mock.call_count, 1)
        send_task_mock.assert_called_once_with("payments.process_outbox", args=["charge_1"])

    async def test_requeue_pending_outbox_releases_lease_after_enqueue_error(self):
        payments = _load_payments_module()
        state = [
            {"charge_id": "charge_2", "status": "pending", "lease_token": None, "leased_at": None, "lease_attempts": 0, "last_error": None}
        ]

        with (
            patch.object(payments, "session_scope", _FakeSessionFactory(state)),
            patch.object(payments.celery, "send_task", side_effect=[Exception("broker down"), None]) as send_task_mock,
            patch.object(payments, "update", _fake_update),
        ):
            first_enqueued, first_errors = await payments.requeue_pending_outbox(batch_size=20)
            second_enqueued, second_errors = await payments.requeue_pending_outbox(batch_size=20)

        self.assertEqual(first_enqueued, 0)
        self.assertEqual(first_errors, 1)
        self.assertEqual(second_enqueued, 1)
        self.assertEqual(second_errors, 0)
        self.assertEqual(send_task_mock.call_count, 2)
        self.assertEqual(state[0]["lease_attempts"], 2)
        self.assertIsNotNone(state[0]["lease_token"])
        self.assertEqual(state[0]["last_error"], "broker down")


class RequeueAppliedUnnotifiedOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_requeue_applied_unnotified_outbox_enqueues_only_unnotified_applied(self):
        payments = _load_payments_module()
        state = [
            {"charge_id": "charge_applied", "status": "applied", "lease_token": None, "leased_at": None, "lease_attempts": 0, "last_error": None, "notified_at": None},
            {"charge_id": "charge_notified", "status": "applied", "lease_token": None, "leased_at": None, "lease_attempts": 0, "last_error": None, "notified_at": "now"},
        ]

        with (
            patch.object(payments, "session_scope", _FakeSessionFactory(state)),
            patch.object(payments.celery, "send_task") as send_task_mock,
            patch.object(payments, "update", _fake_update),
        ):
            enqueued, errors = await payments.requeue_applied_unnotified_outbox(batch_size=20)

        self.assertEqual(enqueued, 1)
        self.assertEqual(errors, 0)
        send_task_mock.assert_called_once_with("payments.process_outbox", args=["charge_applied"])

    async def test_requeue_applied_unnotified_outbox_releases_lease_on_enqueue_error(self):
        payments = _load_payments_module()
        state = [
            {"charge_id": "charge_applied_2", "status": "applied", "lease_token": None, "leased_at": None, "lease_attempts": 0, "last_error": None, "notified_at": None}
        ]

        with (
            patch.object(payments, "session_scope", _FakeSessionFactory(state)),
            patch.object(payments.celery, "send_task", side_effect=[Exception("broker down"), None]) as send_task_mock,
            patch.object(payments, "update", _fake_update),
        ):
            first_enqueued, first_errors = await payments.requeue_applied_unnotified_outbox(batch_size=20)
            second_enqueued, second_errors = await payments.requeue_applied_unnotified_outbox(batch_size=20)

        self.assertEqual(first_enqueued, 0)
        self.assertEqual(first_errors, 1)
        self.assertEqual(second_enqueued, 1)
        self.assertEqual(second_errors, 0)
        self.assertEqual(send_task_mock.call_count, 2)
        self.assertEqual(state[0]["lease_attempts"], 2)
        self.assertIsNone(state[0]["notified_at"])
        self.assertIsNotNone(state[0]["lease_token"])
        self.assertEqual(state[0]["last_error"], "broker down")


if __name__ == "__main__":
    unittest.main()
