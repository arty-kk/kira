import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
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


class _FakeRefundColumn:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return _FakeRefundWhereValue(self.name, other)


class _FakeRefundWhereValue:
    def __init__(self, field, value):
        self.left = type("_Left", (), {"name": field})()
        self.right = type("_Right", (), {"value": value})()


class _FakeRefundOutbox:
    id = _FakeRefundColumn("id")
    status = _FakeRefundColumn("status")
    lease_token = _FakeRefundColumn("lease_token")


class _FakeRefundReleaseStmt:
    table = _FakeRefundOutbox

    def __init__(self):
        self._where_criteria = []
        self._values = {}

    def where(self, *criteria):
        self._where_criteria = criteria
        return self

    def values(self, **values):
        self._values = values
        return self


def _fake_refund_update(_model):
    return _FakeRefundReleaseStmt()


class _FakeRefundDB:
    def __init__(self, state):
        self._state = state

    async def execute(self, stmt, params=None):
        stmt_text = str(stmt)
        if "RETURNING id, lease_token" in stmt_text:
            batch_size = int(params["batch_size"])
            lease_token = params["lease_token"]
            claimed = []
            for row in self._state:
                if row["status"] != "pending" or row["lease_token"] is not None:
                    continue
                row["lease_token"] = lease_token
                row["leased_at"] = "now"
                row["lease_attempts"] += 1
                claimed.append((row["id"], row["lease_token"]))
                if len(claimed) >= batch_size:
                    break
            return _FakeClaimResult(claimed)

        if getattr(stmt, "table", None) is _FakeRefundOutbox:
            outbox_id = int(stmt._where_criteria[0].right.value)
            lease_token = str(stmt._where_criteria[2].right.value)
            values = stmt._values
            for row in self._state:
                if row["id"] == outbox_id and row["status"] == "pending" and row["lease_token"] == lease_token:
                    row["leased_at"] = values.get("leased_at")
                    row["lease_token"] = values.get("lease_token")
                    row["last_error"] = values.get("last_error")
            return _FakeClaimResult([])

        raise AssertionError(f"Unexpected statement: {stmt_text}")


class _FakeRefundSessionFactory:
    def __init__(self, state):
        self._state = state

    @asynccontextmanager
    async def __call__(self, *_args, **_kwargs):
        yield _FakeRefundDB(self._state)


class _SharedRequeueResult(tuple):
    __slots__ = ()
    _fields = ("enqueued", "enqueue_errors")

    def __new__(cls, enqueued, enqueue_errors):
        return super().__new__(cls, (enqueued, enqueue_errors))

    @property
    def enqueued(self):
        return self[0]

    @property
    def enqueue_errors(self):
        return self[1]


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
        "app.tasks.requeue_result": types.ModuleType("app.tasks.requeue_result"),
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
    target_modules["app.tasks.celery_app"]._run = lambda _coro, timeout=None: None
    target_modules["app.tasks.requeue_result"].RequeueResult = _SharedRequeueResult

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


def _load_refunds_module():
    module_name = "tasks_refunds_requeue_under_test"
    target_modules = {
        "app": types.ModuleType("app"),
        "app.core": types.ModuleType("app.core"),
        "app.core.db": types.ModuleType("app.core.db"),
        "app.core.models": types.ModuleType("app.core.models"),
        "app.tasks": types.ModuleType("app.tasks"),
        "app.tasks.celery_app": types.ModuleType("app.tasks.celery_app"),
        "app.tasks.requeue_result": types.ModuleType("app.tasks.requeue_result"),
        "app.services": types.ModuleType("app.services"),
        "app.services.user": types.ModuleType("app.services.user"),
        "app.services.user.user_service": types.ModuleType("app.services.user.user_service"),
    }

    @asynccontextmanager
    async def _dummy_session_scope(*_args, **_kwargs):
        yield None

    class _Celery:
        def task(self, *_args, **_kwargs):
            def _decorator(func):
                return func

            return _decorator

        def send_task(self, *_args, **_kwargs):
            return None

    target_modules["app.core.db"].session_scope = _dummy_session_scope
    target_modules["app.core.models"].RefundOutbox = _FakeRefundOutbox
    target_modules["app.tasks.celery_app"].celery = _Celery()
    target_modules["app.tasks.celery_app"]._run = lambda _coro, timeout=None: None

    target_modules["app.tasks.requeue_result"].RequeueResult = _SharedRequeueResult
    target_modules["app.services.user.user_service"].InvalidBillingTierError = RuntimeError
    target_modules["app.services.user.user_service"].refund_user_balance = AsyncMock()

    previous = {}
    names = set(target_modules) | {module_name}
    for name in names:
        previous[name] = sys.modules.get(name)
        sys.modules.pop(name, None)

    try:
        sys.modules.update(target_modules)
        path = pathlib.Path(__file__).resolve().parents[1] / "app" / "tasks" / "refunds.py"
        spec = importlib.util.spec_from_file_location(module_name, path)
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
            first_result = await payments.requeue_pending_outbox(batch_size=20)
            second_result = await payments.requeue_pending_outbox(batch_size=20)

        self.assertEqual(first_result.enqueued, 1)
        self.assertEqual(first_result.enqueue_errors, 0)
        self.assertEqual(second_result.enqueued, 0)
        self.assertEqual(second_result.enqueue_errors, 0)
        self.assertEqual(send_task_mock.call_count, 1)
        send_task_mock.assert_called_once_with("payments.process_outbox", args=["charge_1", state[0]["lease_token"]])

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
            first_result = await payments.requeue_pending_outbox(batch_size=20)
            second_result = await payments.requeue_pending_outbox(batch_size=20)

        self.assertEqual(first_result.enqueued, 0)
        self.assertEqual(first_result.enqueue_errors, 1)
        self.assertEqual(second_result.enqueued, 1)
        self.assertEqual(second_result.enqueue_errors, 0)
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
        send_task_mock.assert_called_once_with("payments.process_outbox", args=["charge_applied", state[0]["lease_token"]])

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


class RequeueContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_payments_and_refunds_requeue_share_result_contract(self):
        payments = _load_payments_module()
        refunds = _load_refunds_module()

        payment_state = [
            {"charge_id": "charge_contract_1", "status": "pending", "lease_token": None, "leased_at": None, "lease_attempts": 0, "last_error": None},
            {"charge_id": "charge_contract_2", "status": "pending", "lease_token": None, "leased_at": None, "lease_attempts": 0, "last_error": None},
        ]
        refund_state = [
            {"id": 101, "status": "pending", "lease_token": None, "leased_at": None, "lease_attempts": 0, "last_error": None},
            {"id": 102, "status": "pending", "lease_token": None, "leased_at": None, "lease_attempts": 0, "last_error": None},
        ]

        with (
            patch.object(payments, "session_scope", _FakeSessionFactory(payment_state)),
            patch.object(payments.celery, "send_task", side_effect=[Exception("broker down"), None]),
            patch.object(payments, "update", _fake_update),
            patch.object(refunds, "session_scope", _FakeRefundSessionFactory(refund_state)),
            patch.object(refunds.celery, "send_task", side_effect=[Exception("broker down"), None]),
            patch.object(refunds, "update", _fake_refund_update),
        ):
            payments_result = await payments.requeue_pending_outbox(batch_size=2)
            refunds_result = await refunds.requeue_pending_refund_outbox(batch_size=2)

        self.assertIs(type(payments_result), type(refunds_result))
        self.assertEqual(payments_result._fields, ("enqueued", "enqueue_errors"))
        self.assertEqual(refunds_result._fields, ("enqueued", "enqueue_errors"))
        self.assertEqual(payments_result.enqueued, 1)
        self.assertEqual(payments_result.enqueue_errors, 1)
        self.assertEqual(refunds_result.enqueued, 1)
        self.assertEqual(refunds_result.enqueue_errors, 1)


class _ApplyOutboxResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ApplyOutboxDB:
    def __init__(self, outbox, receipt_id):
        self.outbox = outbox
        self.receipt_id = receipt_id
        self.execute_calls = 0

    async def execute(self, _stmt, _params=None):
        self.execute_calls += 1
        if self.execute_calls == 1:
            return _ApplyOutboxResult(self.outbox)
        if self.execute_calls == 2:
            return _ApplyOutboxResult(self.receipt_id)
        raise AssertionError("Unexpected execute call")

    async def get(self, _model, _user_id):
        return SimpleNamespace(id=1)

    async def flush(self):
        return None

    async def refresh(self, _user):
        return None


class _ApplySelectChain:
    def where(self, *_args, **_kwargs):
        return self

    def with_for_update(self):
        return self


class _FakeReceiptInsert:
    def __init__(self, _model, capture):
        self.capture = capture

    def values(self, **_kwargs):
        return self

    def on_conflict_do_nothing(self, *, index_elements):
        self.capture["index_elements"] = list(index_elements)
        return self

    def returning(self, *_args, **_kwargs):
        return self


class ProcessOutboxLeaseTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_outbox_skips_stale_lease_without_mutation(self):
        payments = _load_payments_module()
        outbox = SimpleNamespace(
            telegram_payment_charge_id="charge_race",
            lease_token="fresh-token",
            leased_at=datetime.now(timezone.utc),
            status="pending",
            attempts=3,
            last_error="old",
            applied_at=None,
        )
        db = _ApplyOutboxDB(outbox=outbox, receipt_id=None)

        @asynccontextmanager
        async def _fake_session_scope(*_args, **_kwargs):
            yield db

        with (
            patch.object(payments, "session_scope", _fake_session_scope),
            patch.object(payments, "select", lambda *_args, **_kwargs: _ApplySelectChain()),
            patch.object(payments.logger, "info") as info_mock,
        ):
            result = await payments._apply_outbox("charge_race", "stale-token")

        self.assertEqual(result, (None, None, False))
        self.assertEqual(outbox.attempts, 3)
        self.assertEqual(outbox.status, "pending")
        self.assertEqual(outbox.last_error, "old")
        self.assertIsNone(outbox.applied_at)
        self.assertEqual(db.execute_calls, 1)
        info_mock.assert_any_call(
            "payment_outbox: stale lease skip charge_id=%s expected_lease_token=%s actual_lease_token=%s",
            "charge_race",
            "stale-token",
            "fresh-token",
        )

    async def test_apply_outbox_with_valid_lease_keeps_receipt_idempotency_guard(self):
        payments = _load_payments_module()
        outbox = SimpleNamespace(
            telegram_payment_charge_id="charge_ok",
            provider_payment_charge_id="prov",
            user_id=1,
            kind="buy",
            status="pending",
            requests_amount=2,
            stars_amount=10,
            invoice_payload="buy_2",
            lease_token="fresh-token",
            leased_at=datetime.now(timezone.utc),
            attempts=0,
            last_error="some",
            applied_at=None,
        )
        db = _ApplyOutboxDB(outbox=outbox, receipt_id=None)
        insert_capture = {}

        @asynccontextmanager
        async def _fake_session_scope(*_args, **_kwargs):
            yield db

        with (
            patch.object(payments, "session_scope", _fake_session_scope),
            patch.object(payments, "select", lambda *_args, **_kwargs: _ApplySelectChain()),
            patch.object(payments, "pg_insert", lambda model: _FakeReceiptInsert(model, insert_capture)),
            patch.object(payments, "compute_remaining", lambda _user: 11),
            patch.object(payments, "add_paid_requests", AsyncMock()) as add_paid_requests_mock,
        ):
            row, remaining, duplicate = await payments._apply_outbox("charge_ok", "fresh-token")

        self.assertIs(row, outbox)
        self.assertEqual(remaining, 11)
        self.assertTrue(duplicate)
        self.assertEqual(outbox.attempts, 1)
        self.assertEqual(outbox.status, "applied")
        self.assertIsNotNone(outbox.applied_at)
        self.assertEqual(outbox.lease_token, "fresh-token")
        self.assertEqual(insert_capture["index_elements"], ["telegram_payment_charge_id"])
        add_paid_requests_mock.assert_not_awaited()


class ProcessOutboxRetryNotifyTests(unittest.TestCase):
    def test_process_outbox_retry_with_same_lease_token_reaches_notify_without_requeue(self):
        payments = _load_payments_module()
        outbox = SimpleNamespace(status="applied", notified_at=None)
        apply_mock = AsyncMock(side_effect=[(outbox, 9, False), (outbox, 9, True)])
        notify_mock = AsyncMock(side_effect=[RuntimeError("send failed"), None])

        def _run_sync(coro, timeout=None):
            return asyncio.run(coro)

        with (
            patch.object(payments, "_apply_outbox", apply_mock),
            patch.object(payments, "_notify_payment_result", notify_mock),
            patch.object(payments, "_run", _run_sync),
            patch.object(payments, "requeue_applied_unnotified_outbox", AsyncMock()) as requeue_mock,
        ):
            with self.assertRaises(RuntimeError):
                payments.process_outbox_task("charge_retry", "lease-token")
            payments.process_outbox_task("charge_retry", "lease-token")

        self.assertEqual(apply_mock.await_count, 2)
        apply_mock.assert_any_await("charge_retry", "lease-token")
        self.assertEqual(notify_mock.await_count, 2)
        self.assertEqual(notify_mock.await_args_list[0].args[3], "lease-token")
        self.assertEqual(notify_mock.await_args_list[1].args[3], "lease-token")
        requeue_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
