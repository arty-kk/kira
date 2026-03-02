import asyncio
import copy
import importlib.util
import pathlib
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch


class _FakeColumn:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return _FakeWhereValue(self.name, "eq", other)

    def __ge__(self, other):
        return _FakeWhereValue(self.name, "ge", other)

    def is_not(self, other):
        return _FakeWhereValue(self.name, "is_not", other)


class _FakeModel:
    id = _FakeColumn("id")


class _FakeRefundOutboxModel(_FakeModel):
    status = _FakeColumn("status")
    lease_token = _FakeColumn("lease_token")
    leased_at = _FakeColumn("leased_at")


class _FakeUserModel(_FakeModel):
    pass


class _SelectStmt:
    def __init__(self, model):
        self.model = model
        self._where_criteria = []

    def where(self, *args, **_kwargs):
        self._where_criteria = args
        return self

    def with_for_update(self):
        return self


class _DummyResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ClaimResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeWhereValue:
    def __init__(self, field, operator, value):
        self.left = type("_Left", (), {"name": field})()
        self.operator = operator
        self.right = type("_Right", (), {"value": value})()


class _FakeReleaseStmt:
    table = _FakeRefundOutboxModel

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


class _NestedTx:
    def __init__(self, state):
        self._state = state
        self._snapshot = None

    async def __aenter__(self):
        self._snapshot = {
            "user": copy.deepcopy(self._state["user"]),
            "outbox": copy.deepcopy(self._state["outbox"]),
        }
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        if exc_type is not None:
            self._state["user"].__dict__.clear()
            self._state["user"].__dict__.update(self._snapshot["user"].__dict__)
            self._state["outbox"].__dict__.clear()
            self._state["outbox"].__dict__.update(self._snapshot["outbox"].__dict__)
        return False


class _FakeDB:
    def __init__(self, state):
        self._state = state

    async def execute(self, stmt):
        if getattr(stmt, "model", None) is _FakeRefundOutboxModel:
            outbox = self._state["outbox"]
            for criterion in getattr(stmt, "_where_criteria", []):
                if not hasattr(criterion, "left"):
                    continue
                current_value = getattr(outbox, criterion.left.name)
                expected_value = criterion.right.value
                if criterion.operator == "eq" and current_value != expected_value:
                    return _DummyResult(None)
                if criterion.operator == "is_not" and current_value is expected_value:
                    return _DummyResult(None)
                if criterion.operator == "ge" and current_value < expected_value:
                    return _DummyResult(None)
            return _DummyResult(outbox)
        if getattr(stmt, "model", None) is _FakeUserModel:
            return _DummyResult(self._state["user"])
        raise AssertionError(f"Unexpected statement: {stmt}")

    async def flush(self):
        return None

    def begin_nested(self):
        return _NestedTx(self._state)


class _FakeSessionFactory:
    def __init__(self, committed_state):
        self._committed_state = committed_state

    @asynccontextmanager
    async def __call__(self, *_args, **_kwargs):
        tx_state = copy.deepcopy(self._committed_state)
        db = _FakeDB(tx_state)
        try:
            yield db
        except Exception:
            raise
        else:
            self._committed_state.clear()
            self._committed_state.update(tx_state)


class _FakeRequeueDB:
    def __init__(self, state):
        self._state = state

    async def execute(self, stmt, params=None):
        stmt_text = str(stmt)
        if "RETURNING id, lease_token" in stmt_text:
            batch_size = int(params["batch_size"])
            lease_token = params["lease_token"]
            claimed = []
            for row in self._state:
                if row["status"] != "pending":
                    continue
                if row["lease_token"] is not None:
                    continue
                row["lease_token"] = lease_token
                row["leased_at"] = "now"
                row["lease_attempts"] += 1
                claimed.append((row["id"], row["lease_token"]))
                if len(claimed) >= batch_size:
                    break
            return _ClaimResult(claimed)

        if getattr(stmt, "table", None) is _FakeRefundOutboxModel:
            outbox_id = int(stmt._where_criteria[0].right.value)
            lease_token = str(stmt._where_criteria[2].right.value)
            values = stmt._values
            for row in self._state:
                if row["id"] == outbox_id and row["status"] == "pending" and row["lease_token"] == lease_token:
                    row["leased_at"] = values.get("leased_at")
                    row["lease_token"] = values.get("lease_token")
                    row["last_error"] = values.get("last_error")
            return _ClaimResult([])

        raise AssertionError(f"Unexpected statement: {stmt_text}")


class _FakeRequeueSessionFactory:
    def __init__(self, state):
        self._state = state

    @asynccontextmanager
    async def __call__(self, *_args, **_kwargs):
        yield _FakeRequeueDB(self._state)


def _load_user_service_module(fake_user_model):
    module_name = "user_service_under_test"
    target_modules = {
        "app": types.ModuleType("app"),
        "app.core": types.ModuleType("app.core"),
        "app.core.db": types.ModuleType("app.core.db"),
        "app.core.models": types.ModuleType("app.core.models"),
        "aiogram": types.ModuleType("aiogram"),
        "aiogram.types": types.ModuleType("aiogram.types"),
    }

    @asynccontextmanager
    async def _dummy_session_scope(*_args, **_kwargs):
        yield None

    target_modules["app.core.db"].session_scope = _dummy_session_scope
    target_modules["app.core.models"].User = fake_user_model
    target_modules["app.core.models"].RequestReservation = type("_FakeReservation", (), {})
    target_modules["aiogram.types"].User = type("_FakeTelegramUser", (), {})

    previous = {}
    names = set(target_modules) | {module_name}
    for name in names:
        previous[name] = sys.modules.get(name)
        sys.modules.pop(name, None)

    try:
        sys.modules.update(target_modules)
        path = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "user" / "user_service.py"
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        module.select = lambda model: _SelectStmt(model)
        return module
    finally:
        for name in names:
            sys.modules.pop(name, None)
            if previous[name] is not None:
                sys.modules[name] = previous[name]


def _load_refunds_module():
    user_service = _load_user_service_module(_FakeUserModel)
    module_name = "tasks_refunds_under_test"
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
    }


    @asynccontextmanager
    async def _dummy_session_scope(*_args, **_kwargs):
        yield None

    target_modules["app.core.db"].session_scope = _dummy_session_scope
    target_modules["app.core.models"].RefundOutbox = _FakeRefundOutboxModel
    target_modules["app.core.models"].User = _FakeUserModel

    class _Celery:
        def task(self, *_args, **_kwargs):
            def _decorator(func):
                return func

            return _decorator

        def send_task(self, *_args, **_kwargs):
            return None

    target_modules["app.tasks.celery_app"].celery = _Celery()
    target_modules["app.tasks.celery_app"].run_coro_sync = lambda _coro, timeout=None: None

    class _RequeueResult(tuple):
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

    target_modules["app.tasks.requeue_result"].RequeueResult = _RequeueResult
    target_modules["app.services.user.user_service"] = user_service

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


class RefundOutboxAtomicityTests(unittest.TestCase):
    def test_worker_partial_failure_rolls_back_balance_and_retry_applies_once(self):
        refunds = _load_refunds_module()
        state = {
            "user": SimpleNamespace(id=10, free_requests=0, paid_requests=0, used_requests=1),
            "outbox": SimpleNamespace(
                id=7,
                owner_id=10,
                billing_tier="free",
                request_id="req-1",
                status="pending",
                attempts=0,
                lease_attempts=0,
                leased_at=datetime.now(timezone.utc),
                lease_token="token",
                last_error=None,
                processed_at=None,
            ),
        }

        original_refund = refunds._refund_balance_for_outbox
        call_count = 0

        async def _refund_then_fail_once(db, owner_id, billing_tier):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await original_refund(db, owner_id, billing_tier)
                raise RuntimeError("after refund")
            await original_refund(db, owner_id, billing_tier)

        with (
            patch.object(refunds, "session_scope", _FakeSessionFactory(state)),
            patch.object(refunds, "select", side_effect=lambda model: _SelectStmt(model)),
            patch.object(refunds, "run_coro_sync", side_effect=lambda coro: asyncio.run(coro)),
            patch.object(refunds, "_refund_balance_for_outbox", side_effect=_refund_then_fail_once),
        ):
            refunds.process_refund_outbox_task(7, "token")

        self.assertEqual(state["user"].free_requests, 0)
        self.assertEqual(state["user"].used_requests, 1)
        self.assertEqual(state["outbox"].status, "pending")
        self.assertEqual(state["outbox"].attempts, 1)
        self.assertIsNone(state["outbox"].leased_at)
        self.assertIsNone(state["outbox"].lease_token)
        self.assertIsNotNone(state["outbox"].last_error)

        state["outbox"].lease_token = "token-2"
        state["outbox"].leased_at = datetime.now(timezone.utc)

        with (
            patch.object(refunds, "session_scope", _FakeSessionFactory(state)),
            patch.object(refunds, "select", side_effect=lambda model: _SelectStmt(model)),
            patch.object(refunds, "run_coro_sync", side_effect=lambda coro: asyncio.run(coro)),
            patch.object(refunds, "_refund_balance_for_outbox", side_effect=_refund_then_fail_once),
        ):
            refunds.process_refund_outbox_task(7, "token-2")

        self.assertEqual(state["user"].free_requests, 1)
        self.assertEqual(state["user"].used_requests, 0)
        self.assertEqual(state["outbox"].attempts, 2)
        self.assertEqual(state["outbox"].status, "applied")
        self.assertIsNotNone(state["outbox"].processed_at)



    def test_direct_and_outbox_refund_paths_are_equivalent(self):
        refunds = _load_refunds_module()

        for billing_tier in ("free", "paid"):
            direct_state = {
                "user": SimpleNamespace(id=10, free_requests=2, paid_requests=3, used_requests=0),
                "outbox": None,
            }
            outbox_state = copy.deepcopy(direct_state)

            asyncio.run(refunds.refund_user_balance(_FakeDB(direct_state), 10, billing_tier))
            asyncio.run(refunds._refund_balance_for_outbox(_FakeDB(outbox_state), 10, billing_tier))

            self.assertEqual(direct_state["user"].free_requests, outbox_state["user"].free_requests)
            self.assertEqual(direct_state["user"].paid_requests, outbox_state["user"].paid_requests)
            self.assertEqual(direct_state["user"].used_requests, outbox_state["user"].used_requests)

        missing_user_state = {"user": None, "outbox": None}
        asyncio.run(refunds.refund_user_balance(_FakeDB(missing_user_state), 999, "free"))
        asyncio.run(refunds._refund_balance_for_outbox(_FakeDB({"user": None, "outbox": None}), 999, "free"))

        with self.assertRaises(refunds.InvalidBillingTierError):
            asyncio.run(refunds.refund_user_balance(_FakeDB({"user": None, "outbox": None}), 10, "bad"))
        with self.assertRaises(refunds.InvalidBillingTierError):
            asyncio.run(refunds._refund_balance_for_outbox(_FakeDB({"user": None, "outbox": None}), 10, "bad"))

    def test_invalid_billing_tier_marks_failed_without_balance_mutation(self):
        refunds = _load_refunds_module()
        state = {
            "user": SimpleNamespace(id=10, free_requests=2, paid_requests=3, used_requests=4),
            "outbox": SimpleNamespace(
                id=8,
                owner_id=10,
                billing_tier="bad",
                request_id="req-invalid",
                status="pending",
                attempts=0,
                lease_attempts=0,
                leased_at=datetime.now(timezone.utc),
                lease_token="token",
                last_error=None,
                processed_at=None,
            ),
        }

        with (
            patch.object(refunds, "session_scope", _FakeSessionFactory(state)),
            patch.object(refunds, "select", side_effect=lambda model: _SelectStmt(model)),
            patch.object(refunds, "run_coro_sync", side_effect=lambda coro: asyncio.run(coro)),
        ):
            refunds.process_refund_outbox_task(8, "token")

        self.assertEqual(state["outbox"].status, "failed")
        self.assertEqual(state["outbox"].last_error, "invalid_billing_tier")
        self.assertIsNone(state["outbox"].processed_at)
        self.assertEqual(state["user"].free_requests, 2)
        self.assertEqual(state["user"].paid_requests, 3)
        self.assertEqual(state["user"].used_requests, 4)

    def test_transient_error_exceeding_attempt_limit_marks_failed(self):
        refunds = _load_refunds_module()
        state = {
            "user": SimpleNamespace(id=10, free_requests=0, paid_requests=0, used_requests=1),
            "outbox": SimpleNamespace(
                id=9,
                owner_id=10,
                billing_tier="free",
                request_id="req-retry-limit",
                status="pending",
                attempts=refunds.REFUND_OUTBOX_MAX_ATTEMPTS - 1,
                lease_attempts=0,
                leased_at=datetime.now(timezone.utc),
                lease_token="token",
                last_error=None,
                processed_at=None,
            ),
        }

        async def _always_fail(*_args, **_kwargs):
            raise RuntimeError("transient")

        with (
            patch.object(refunds, "session_scope", _FakeSessionFactory(state)),
            patch.object(refunds, "select", side_effect=lambda model: _SelectStmt(model)),
            patch.object(refunds, "run_coro_sync", side_effect=lambda coro: asyncio.run(coro)),
            patch.object(refunds, "_refund_balance_for_outbox", side_effect=_always_fail),
        ):
            refunds.process_refund_outbox_task(9, "token")

        self.assertEqual(state["outbox"].attempts, refunds.REFUND_OUTBOX_MAX_ATTEMPTS)
        self.assertEqual(state["outbox"].status, "failed")
        self.assertEqual(state["outbox"].last_error, "RuntimeError('transient')")
        self.assertIsNone(state["outbox"].processed_at)


    def test_reprocessing_applied_outbox_does_not_refund_twice(self):
        refunds = _load_refunds_module()
        state = {
            "user": SimpleNamespace(id=10, free_requests=0, paid_requests=0, used_requests=1),
            "outbox": SimpleNamespace(
                id=13,
                owner_id=10,
                billing_tier="free",
                request_id="req-idem",
                status="pending",
                attempts=0,
                lease_attempts=0,
                leased_at=datetime.now(timezone.utc),
                lease_token="token",
                last_error=None,
                processed_at=None,
            ),
        }

        with (
            patch.object(refunds, "session_scope", _FakeSessionFactory(state)),
            patch.object(refunds, "select", side_effect=lambda model: _SelectStmt(model)),
            patch.object(refunds, "run_coro_sync", side_effect=lambda coro: asyncio.run(coro)),
        ):
            refunds.process_refund_outbox_task(13, "token")

        self.assertEqual(state["user"].free_requests, 1)
        self.assertEqual(state["user"].used_requests, 0)
        self.assertEqual(state["outbox"].status, "applied")

        with (
            patch.object(refunds, "session_scope", _FakeSessionFactory(state)),
            patch.object(refunds, "select", side_effect=lambda model: _SelectStmt(model)),
            patch.object(refunds, "run_coro_sync", side_effect=lambda coro: asyncio.run(coro)),
        ):
            refunds.process_refund_outbox_task(13, "token")

        self.assertEqual(state["user"].free_requests, 1)
        self.assertEqual(state["user"].used_requests, 0)
        self.assertEqual(state["outbox"].attempts, 1)

    def test_stale_lease_token_does_not_change_outbox_state(self):
        refunds = _load_refunds_module()
        stale_lease_time = datetime.now(timezone.utc) - timedelta(seconds=refunds.REFUND_OUTBOX_LEASE_TTL_SECONDS + 1)
        state = {
            "user": SimpleNamespace(id=10, free_requests=0, paid_requests=0, used_requests=1),
            "outbox": SimpleNamespace(
                id=14,
                owner_id=10,
                billing_tier="free",
                request_id="req-stale-lease",
                status="pending",
                attempts=1,
                lease_attempts=1,
                leased_at=stale_lease_time,
                lease_token="new-token",
                last_error="prev",
                processed_at=None,
            ),
        }
        snapshot = copy.deepcopy(state["outbox"])

        with (
            patch.object(refunds, "session_scope", _FakeSessionFactory(state)),
            patch.object(refunds, "select", side_effect=lambda model: _SelectStmt(model)),
            patch.object(refunds, "run_coro_sync", side_effect=lambda coro: asyncio.run(coro)),
        ):
            refunds.process_refund_outbox_task(14, "old-token")

        self.assertEqual(state["outbox"].attempts, snapshot.attempts)
        self.assertEqual(state["outbox"].status, snapshot.status)
        self.assertEqual(state["outbox"].leased_at, snapshot.leased_at)
        self.assertEqual(state["outbox"].lease_token, snapshot.lease_token)
        self.assertEqual(state["outbox"].last_error, snapshot.last_error)
        self.assertEqual(state["outbox"].processed_at, snapshot.processed_at)

    def test_requeue_pending_refund_outbox_releases_lease_after_enqueue_error(self):
        refunds = _load_refunds_module()
        state = [
            {"id": 11, "status": "pending", "lease_token": None, "leased_at": None, "lease_attempts": 0, "last_error": None}
        ]

        async def _run_test():
            with (
                patch.object(refunds, "session_scope", _FakeRequeueSessionFactory(state)),
                patch.object(refunds.celery, "send_task", side_effect=[Exception("broker down"), None]) as send_task_mock,
                patch.object(refunds, "update", _fake_update),
            ):
                first_result = await refunds.requeue_pending_refund_outbox(batch_size=20)
                second_result = await refunds.requeue_pending_refund_outbox(batch_size=20)

            self.assertEqual(first_result.enqueued, 0)
            self.assertEqual(first_result.enqueue_errors, 1)
            self.assertEqual(second_result.enqueued, 1)
            self.assertEqual(second_result.enqueue_errors, 0)
            self.assertEqual(send_task_mock.call_count, 2)
            first_call_args = send_task_mock.call_args_list[0].kwargs["args"]
            second_call_args = send_task_mock.call_args_list[1].kwargs["args"]
            self.assertEqual(len(first_call_args), 2)
            self.assertEqual(len(second_call_args), 2)
            self.assertEqual(first_call_args[0], 11)
            self.assertEqual(second_call_args[0], 11)
            self.assertNotEqual(first_call_args[1], second_call_args[1])
            self.assertEqual(second_call_args[1], state[0]["lease_token"])
            self.assertEqual(state[0]["lease_attempts"], 2)
            self.assertIsNotNone(state[0]["lease_token"])
            self.assertEqual(state[0]["last_error"], "broker down")

        asyncio.run(_run_test())


if __name__ == "__main__":
    unittest.main()
