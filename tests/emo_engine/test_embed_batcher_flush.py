import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


def _seed_env() -> None:
    os.environ.setdefault("OPENAI_API_KEY", "test")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCdef1234567890")
    os.environ.setdefault("TELEGRAM_BOT_USERNAME", "testbot")
    os.environ.setdefault("TELEGRAM_BOT_ID", "1")
    os.environ.setdefault("WEBHOOK_URL", "https://example.invalid")
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
    os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")
    os.environ.setdefault("TWITTER_API_KEY", "test")
    os.environ.setdefault("TWITTER_API_SECRET", "test")
    os.environ.setdefault("TWITTER_ACCESS_TOKEN", "test")
    os.environ.setdefault("TWITTER_ACCESS_TOKEN_SECRET", "test")
    os.environ.setdefault("TWITTER_BEARER_TOKEN", "test")


_seed_env()

from app.emo_engine.persona import memory


class _DummyPipe:
    def set(self, *_args, **_kwargs):
        return None

    async def execute(self):
        return None


class _DummyRedis:
    async def mget(self, keys):
        return [None for _ in keys]

    async def delete(self, *_args, **_kwargs):
        return None

    def pipeline(self, transaction=False):
        return _DummyPipe()


class EmbedBatcherFlushTests(unittest.IsolatedAsyncioTestCase):
    async def test_flush_processes_large_queue_in_multiple_batches(self) -> None:
        batcher = memory._EmbedBatcher()
        calls = []

        async def fake_embeddings_call(**kwargs):
            miss_texts = kwargs["input"]
            calls.append(len(miss_texts))
            items = [SimpleNamespace(embedding=[1.0] * memory._DIM) for _ in miss_texts]
            return SimpleNamespace(data=items)

        with patch.object(memory, "_BG_BATCH_MAX", 8), patch.object(memory, "_BG_BATCH_WAIT_MS", 10), patch.object(
            memory,
            "get_redis",
            return_value=_DummyRedis(),
        ), patch.object(memory, "_call_openai_with_retry", side_effect=fake_embeddings_call):
            task_group = [asyncio.create_task(batcher.add(f"text-{i}")) for i in range(40)]
            results = await asyncio.wait_for(asyncio.gather(*task_group), timeout=2)

        self.assertEqual(len(results), 40)
        self.assertTrue(all(result != memory._ZERO_VEC for result in results))
        self.assertGreater(len(calls), 1)
        self.assertEqual(sum(calls), 40)
        self.assertEqual(len(batcher._queue), 0)

    async def test_batch_exception_sets_zero_vector_for_each_future(self) -> None:
        batcher = memory._EmbedBatcher()

        async def failing_embeddings_call(**_kwargs):
            raise RuntimeError("embeddings failed")

        with patch.object(memory, "_BG_BATCH_MAX", 100), patch.object(memory, "_BG_BATCH_WAIT_MS", 1), patch.object(
            memory,
            "get_redis",
            return_value=_DummyRedis(),
        ), patch.object(memory, "_call_openai_with_retry", side_effect=failing_embeddings_call):
            task_group = [asyncio.create_task(batcher.add(f"bad-{i}")) for i in range(6)]
            results = await asyncio.wait_for(asyncio.gather(*task_group), timeout=2)

        self.assertEqual(results, [memory._ZERO_VEC] * 6)
        self.assertEqual(len(batcher._queue), 0)


    async def test_lock_is_not_held_during_external_calls(self) -> None:
        batcher = memory._EmbedBatcher()
        entered_openai = asyncio.Event()
        release_openai = asyncio.Event()

        async def blocking_embeddings_call(**kwargs):
            entered_openai.set()
            await release_openai.wait()
            miss_texts = kwargs["input"]
            items = [SimpleNamespace(embedding=[1.0] * memory._DIM) for _ in miss_texts]
            return SimpleNamespace(data=items)

        with patch.object(memory, "_BG_BATCH_MAX", 2), patch.object(memory, "_BG_BATCH_WAIT_MS", 500), patch.object(
            memory,
            "get_redis",
            return_value=_DummyRedis(),
        ), patch.object(memory, "_call_openai_with_retry", side_effect=blocking_embeddings_call):
            first = asyncio.create_task(batcher.add("text-1"))
            second = asyncio.create_task(batcher.add("text-2"))
            await asyncio.wait_for(entered_openai.wait(), timeout=1)

            third = asyncio.create_task(batcher.add("text-3"))
            await asyncio.sleep(0.05)

            self.assertFalse(third.done())
            self.assertEqual(len(batcher._queue), 1)

            release_openai.set()
            results = await asyncio.wait_for(asyncio.gather(first, second, third), timeout=2)

        self.assertEqual(len(results), 3)
        self.assertTrue(all(result != memory._ZERO_VEC for result in results))


if __name__ == "__main__":
    unittest.main()
