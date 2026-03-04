import unittest

from app.bot.components.constants import _LazyClient


class ConstantsLazyClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_lazy_client_does_not_cache_client_process_wide(self) -> None:
        class _Client:
            def __init__(self, marker: int) -> None:
                self.marker = marker

            async def ping(self) -> int:
                return self.marker

        state = {"calls": 0}

        def _factory() -> _Client:
            state["calls"] += 1
            return _Client(state["calls"])

        lazy_client = _LazyClient(_factory)

        first = await lazy_client.ping()
        second = await lazy_client.ping()

        self.assertEqual(first, 1)
        self.assertEqual(second, 2)
        self.assertEqual(state["calls"], 2)

    async def test_lazy_client_defers_factory_until_coroutine_execution(self) -> None:
        state = {"in_loop": False}

        class _AsyncClient:
            async def get(self, key: str) -> str:
                return f"value:{key}"

        def _factory() -> _AsyncClient:
            if not state["in_loop"]:
                raise RuntimeError("get_redis() requires an active asyncio event loop; call it from async context")
            return _AsyncClient()

        lazy_client = _LazyClient(_factory)
        pending = lazy_client.get("foo")

        state["in_loop"] = True
        result = await pending

        self.assertEqual(result, "value:foo")


if __name__ == "__main__":
    unittest.main()
