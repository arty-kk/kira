from __future__ import annotations

import asyncio
import os
import tempfile

from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator




async def create_temp_path(*, suffix: str) -> str:
    def _create() -> str:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            return tmp.name

    return await asyncio.to_thread(_create)

async def write_temp_bytes(*, data: bytes, suffix: str) -> str:
    def _write() -> str:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            return tmp.name

    return await asyncio.to_thread(_write)


async def open_binary_read(path: str):
    return await asyncio.to_thread(open, path, "rb")


@asynccontextmanager
async def managed_temp_file(*, data: bytes | None = None, suffix: str = "") -> AsyncIterator[str]:
    path = await (write_temp_bytes(data=data, suffix=suffix) if data is not None else create_temp_path(suffix=suffix))
    try:
        yield path
    finally:
        with suppress(Exception):
            await asyncio.to_thread(os.remove, path)

