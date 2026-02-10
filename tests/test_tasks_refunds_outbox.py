import asyncio
import copy
import importlib.util
import pathlib
import sys
import types
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch


class _FakeColumn:
    def __eq__(self, _other):
        return self


class _FakeModel:
    id = _FakeColumn()


class _FakeRefundOutboxModel(_FakeModel):
    status = _FakeColumn()


class _FakeUserModel(_FakeModel):
    pass


class _SelectStmt:
    def __init__(self, model):
        self.model = model

    def where(self, *_args, **_kwargs):
        return self

    def with_for_update(self):
        return self


class _DummyResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


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
            return _DummyResult(self._state["outbox"])
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


def _load_refunds_module():
    module_name = "tasks_refunds_under_test"
    target_modules = {
        "app": types.ModuleType("app"),
        "app.core": types.ModuleType("app.core"),
        "app.core.db": types.ModuleType("app.core.db"),
        "app.core.models": types.ModuleType("app.core.models"),
        "app.tasks": types.ModuleType("app.tasks"),
        "app.tasks.celery_app": types.ModuleType("app.tasks.celery_app"),
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
    target_modules["app.tasks.celery_app"]._run = lambda _coro: None

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
                leased_at="lease",
                lease_token="token",
                last_error=None,
                processed_at=None,
            ),
        }

        original_refund = refunds._refund_balance_for_outbox

        async def _refund_then_fail(db, owner_id, billing_tier):
            await original_refund(db, owner_id, billing_tier)
            raise RuntimeError("after refund")

        with (
            patch.object(refunds, "session_scope", _FakeSessionFactory(state)),
            patch.object(refunds, "select", side_effect=lambda model: _SelectStmt(model)),
            patch.object(refunds, "_run", side_effect=lambda coro: asyncio.run(coro)),
            patch.object(refunds, "_refund_balance_for_outbox", side_effect=_refund_then_fail),
        ):
            refunds.process_refund_outbox_task(7)

        self.assertEqual(state["user"].free_requests, 0)
        self.assertEqual(state["user"].used_requests, 1)
        self.assertEqual(state["outbox"].status, "failed")
        self.assertIsNotNone(state["outbox"].last_error)

        # Эмулируем повторную постановку в обработку после устранения ошибки.
        state["outbox"].status = "pending"
        state["outbox"].last_error = None
        state["outbox"].processed_at = None

        with (
            patch.object(refunds, "session_scope", _FakeSessionFactory(state)),
            patch.object(refunds, "select", side_effect=lambda model: _SelectStmt(model)),
            patch.object(refunds, "_run", side_effect=lambda coro: asyncio.run(coro)),
        ):
            refunds.process_refund_outbox_task(7)

        self.assertEqual(state["user"].free_requests, 1)
        self.assertEqual(state["user"].used_requests, 0)
        self.assertEqual(state["outbox"].status, "applied")
        self.assertIsNotNone(state["outbox"].processed_at)


if __name__ == "__main__":
    unittest.main()
