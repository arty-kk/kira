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

    async def get(self, model, obj_id):
        return self._kb

    async def flush(self):
        return None

    async def execute(self, stmt):
        return _FakeResult()

    def add_all(self, rows):
        self.added.extend(rows)


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

        embed_mock = AsyncMock(return_value=[[0.1, 0.2, 0.3]])

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


if __name__ == "__main__":
    unittest.main()
