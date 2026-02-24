from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def invalidate_api_kb_cache(owner_id: Optional[int] = None) -> None:
    if owner_id is None:
        logger.info("API-KB cache invalidated: full")
        return
    logger.info("API-KB cache invalidated: owner_id=%s", owner_id)
