from __future__ import annotations

from typing import NamedTuple


class RequeueResult(NamedTuple):
    enqueued: int
    enqueue_errors: int

