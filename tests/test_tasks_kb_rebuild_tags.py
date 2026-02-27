import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.tasks import kb


class _FakeResult:
    def fetchall(self):
        return []


class _FakeDb:
    def __init__(self, kb_obj):
        self._kb = kb_obj
        self.added = []
        self.add_all_calls = []
        self.flush_calls = 0

    async def get(self, model, obj_id):
        return self._kb

    async def flush(self):
        self.flush_calls += 1
        return None

    async def execute(self, stmt):
        return _FakeResult()

    def add_all(self, rows):
        rows_list = list(rows)
        self.add_all_calls.append(rows_list)
        self.added.extend(rows_list)


class _SessionScopeCtx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class KbRebuildTagDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebuild_deduplicates_repeated_item_tags_but_keeps_rows_per_item(self):
        kb_obj = SimpleNamespace(
            id=55,
            api_key_id=77,
            items=[
                {"id": "item-1", "text": "First text", "tags": ["foo", "foo", " foo "]},
                {"id": "item-2", "text": "Second text", "tags": ["foo"]},
            ],
            embedding_model="text-embedding-3-large",
            status="new",
            error=None,
            chunks_count=0,
            version=1,
        )

        init_db = _FakeDb(kb_obj)
        persist_db = _FakeDb(kb_obj)
        db_queue = [init_db, persist_db]

        def _fake_session_scope(*args, **kwargs):
            return _SessionScopeCtx(db_queue.pop(0))

        embed_mock = AsyncMock(return_value=[[0.1] * 3072])

        with (
            patch.object(kb, "session_scope", side_effect=_fake_session_scope),
            patch.object(kb, "_embed_texts", embed_mock),
            patch.object(kb, "_notify_kb_status", AsyncMock()),
            patch.object(kb, "invalidate_api_kb_cache"),
            patch.object(kb, "invalidate_tags_index"),
            patch.object(
                kb,
                "settings",
                SimpleNamespace(RAG_VECTOR_DIM=3, MAX_KB_VERSIONS_PER_KEY=0),
            ),
        ):
            await kb._rebuild_for_api_key_async(api_key_id=77, kb_id=55)

        embed_mock.assert_awaited_once_with(["foo"], model="text-embedding-3-large")

        self.assertEqual(len(persist_db.added), 2)
        rows_by_external_id = {row.external_id: row for row in persist_db.added}
        self.assertEqual(set(rows_by_external_id.keys()), {"item-1", "item-2"})
        self.assertEqual(rows_by_external_id["item-1"].tag, "foo")
        self.assertEqual(rows_by_external_id["item-2"].tag, "foo")


class KbRebuildBatchInsertTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebuild_inserts_tag_rows_in_batches(self):
        kb_obj = SimpleNamespace(
            id=57,
            api_key_id=77,
            items=[
                {"id": "item-1", "text": "First text", "tags": ["foo"]},
                {"id": "item-2", "text": "Second text", "tags": ["foo"]},
                {"id": "item-3", "text": "Third text", "tags": ["foo"]},
                {"id": "item-4", "text": "Fourth text", "tags": ["foo"]},
                {"id": "item-5", "text": "Fifth text", "tags": ["foo"]},
            ],
            embedding_model="text-embedding-3-large",
            status="new",
            error=None,
            chunks_count=0,
            version=1,
        )

        init_db = _FakeDb(kb_obj)
        persist_db = _FakeDb(kb_obj)
        db_queue = [init_db, persist_db]

        def _fake_session_scope(*args, **kwargs):
            return _SessionScopeCtx(db_queue.pop(0))

        embed_mock = AsyncMock(return_value=[[0.1] * 3072])

        with (
            patch.object(kb, "session_scope", side_effect=_fake_session_scope),
            patch.object(kb, "_embed_texts", embed_mock),
            patch.object(kb, "_notify_kb_status", AsyncMock()),
            patch.object(kb, "invalidate_api_kb_cache"),
            patch.object(kb, "invalidate_tags_index"),
            patch.object(
                kb,
                "settings",
                SimpleNamespace(RAG_VECTOR_DIM=3, MAX_KB_VERSIONS_PER_KEY=0, KB_TAG_INSERT_BATCH_SIZE=2),
            ),
        ):
            await kb._rebuild_for_api_key_async(api_key_id=77, kb_id=57)

        self.assertEqual(len(persist_db.added), 5)
        self.assertEqual([len(call) for call in persist_db.add_all_calls], [2, 2, 1])
        # per-batch flushes + final flush
        self.assertEqual(persist_db.flush_calls, 4)


class KbRebuildLongFieldsNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebuild_truncates_long_external_id_and_tag(self):
        long_id = "i" * 400
        long_tag = "t" * 400
        kb_obj = SimpleNamespace(
            id=58,
            api_key_id=77,
            items=[{"id": long_id, "text": "First text", "tags": [long_tag]}],
            embedding_model="text-embedding-3-large",
            status="new",
            error=None,
            chunks_count=0,
            version=1,
        )

        init_db = _FakeDb(kb_obj)
        persist_db = _FakeDb(kb_obj)
        db_queue = [init_db, persist_db]

        def _fake_session_scope(*args, **kwargs):
            return _SessionScopeCtx(db_queue.pop(0))

        embed_mock = AsyncMock(return_value=[[0.1] * 3072])

        with (
            patch.object(kb, "session_scope", side_effect=_fake_session_scope),
            patch.object(kb, "_embed_texts", embed_mock),
            patch.object(kb, "_notify_kb_status", AsyncMock()),
            patch.object(kb, "invalidate_api_kb_cache"),
            patch.object(kb, "invalidate_tags_index"),
            patch.object(kb.logger, "info") as log_info,
            patch.object(
                kb,
                "settings",
                SimpleNamespace(RAG_VECTOR_DIM=3, MAX_KB_VERSIONS_PER_KEY=0, KB_TAG_INSERT_BATCH_SIZE=10),
            ),
        ):
            await kb._rebuild_for_api_key_async(api_key_id=77, kb_id=58)

        self.assertEqual(kb_obj.status, "ready")
        self.assertEqual(len(persist_db.added), 1)
        row = persist_db.added[0]
        self.assertEqual(len(row.external_id), 255)
        self.assertEqual(len(row.tag), 255)
        self.assertEqual(row.external_id, long_id[:255])
        self.assertEqual(row.tag, long_tag[:255])

        info_args = [call.args[0] for call in log_info.call_args_list if call.args]
        self.assertTrue(any("kb: tag_rows normalization" in msg for msg in info_args))


class KbRebuildErrorSanitizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebuild_sanitizes_db_error_for_log_and_notification(self):
        kb_obj = SimpleNamespace(
            id=56,
            api_key_id=77,
            items=[{"id": "item-1", "text": "First text", "tags": ["foo"]}],
            embedding_model="text-embedding-3-large",
            status="new",
            error=None,
            chunks_count=0,
            version=1,
        )

        class _FailingPersistDb(_FakeDb):
            async def flush(self):
                raise RuntimeError("SQL INSERT failed values=[EMBEDDING_DUMP_ABC] " + ("x" * 2000))

        init_db = _FakeDb(kb_obj)
        persist_db = _FailingPersistDb(kb_obj)
        update_db = _FakeDb(kb_obj)
        db_queue = [init_db, persist_db, update_db]

        def _fake_session_scope(*args, **kwargs):
            return _SessionScopeCtx(db_queue.pop(0))

        embed_mock = AsyncMock(return_value=[[0.1] * 3072])
        notify_mock = AsyncMock()

        with (
            patch.object(kb, "session_scope", side_effect=_fake_session_scope),
            patch.object(kb, "_embed_texts", embed_mock),
            patch.object(kb, "_notify_kb_status", notify_mock),
            patch.object(kb, "invalidate_api_kb_cache"),
            patch.object(kb, "invalidate_tags_index"),
            patch.object(kb.logger, "error") as log_error,
            patch.object(
                kb,
                "settings",
                SimpleNamespace(RAG_VECTOR_DIM=3, MAX_KB_VERSIONS_PER_KEY=0),
            ),
        ):
            await kb._rebuild_for_api_key_async(api_key_id=77, kb_id=56)

        # user-facing error is sanitized and bounded
        self.assertTrue(notify_mock.await_args is not None)
        err = notify_mock.await_args.kwargs["error"]
        self.assertIn("RuntimeError:", err)
        self.assertNotIn("EMBEDDING_DUMP_ABC", err)
        self.assertLessEqual(len(err), 320)

        # persisted error is same safe message
        self.assertEqual(kb_obj.error, err)

        # logs use safe error only, no raw embedding dump
        msg, *args = log_error.call_args.args
        self.assertIn("err_type=%s", msg)
        self.assertNotIn("EMBEDDING_DUMP_ABC", " ".join(str(x) for x in args))


if __name__ == "__main__":
    unittest.main()
