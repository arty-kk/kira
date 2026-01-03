#app/bot/utils/shop_tiers.py
from __future__ import annotations

from contextlib import suppress
from functools import lru_cache
from typing import Any, Optional, TypedDict

from app.config import settings
from app.bot.i18n import t


class GiftTier(TypedDict):
    code: str
    title: str
    emoji: str
    requests: int
    stars: int


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (bytes, bytearray)):
            v = v.decode("utf-8", "ignore")
        s = str(v).strip()
        if not s:
            return None
        return int(s)
    except Exception:
        return None


def _pick_price_stars(raw: dict[str, Any]) -> int:
    """
    Supports multiple legacy keys:
      - price_stars (preferred)
      - stars
      - price
      - price_cents
    Returns 0 if not parseable / <= 0.
    """
    for k in ("price_stars", "stars", "price", "price_cents"):
        if k in raw:
            val = _to_int(raw.get(k))
            return int(val or 0)
    return 0


@lru_cache(maxsize=1)
def _purchase_tiers_cached() -> tuple[tuple[int, int], ...]:
    raw = getattr(settings, "PURCHASE_TIERS", None) or {}
    out: dict[int, int] = {}

    if isinstance(raw, dict):
        for k, v in raw.items():
            req = _to_int(k)
            stars = _to_int(v)
            if not req or not stars:
                continue
            if req > 0 and stars > 0:
                out[int(req)] = int(stars)

    return tuple(sorted(out.items(), key=lambda kv: int(kv[0])))


def purchase_tiers() -> dict[int, int]:
    # Return a fresh dict to avoid accidental mutation of cached structure semantics.
    return dict(_purchase_tiers_cached())


@lru_cache(maxsize=1)
def _gift_tiers_cached() -> tuple[tuple[str, str, str, int, int], ...]:
    """
    Cached normalized gift tiers: (code, title, emoji, requests, stars)
    """
    tiers = getattr(settings, "GIFT_TIERS", None) or []
    out: list[tuple[str, str, str, int, int]] = []

    if not isinstance(tiers, list):
        return ()

    for raw in tiers:
        if not isinstance(raw, dict):
            continue

        code = str(raw.get("code") or "").strip()
        if not code:
            continue

        title = str(raw.get("title") or code).strip()
        emoji = str(raw.get("emoji") or "").strip()

        req = int(_to_int(raw.get("requests")) or 0)
        stars = int(_pick_price_stars(raw) or 0)

        if req <= 0 or stars <= 0:
            continue

        out.append((code, title, emoji, req, stars))

    # stable, predictable ordering
    out.sort(key=lambda x: (int(x[4]), int(x[3]), str(x[0])))
    return tuple(out)


@lru_cache(maxsize=1)
def _gift_map_cached() -> dict[str, tuple[str, str, int, int]]:
    # code -> (title, emoji, requests, stars)
    return {code: (title, emoji, req, stars) for (code, title, emoji, req, stars) in _gift_tiers_cached()}


def gift_tiers() -> list[GiftTier]:
    # fresh list/dicts so callers can't mutate cached objects
    return [
        {"code": code, "title": title, "emoji": emoji, "requests": int(req), "stars": int(stars)}
        for (code, title, emoji, req, stars) in _gift_tiers_cached()
    ]


def find_gift(code: str) -> GiftTier | None:
    code = (code or "").strip()
    t5 = _gift_map_cached().get(code)
    if not t5:
        return None
    title, emoji, req, stars = t5
    return {"code": code, "title": title, "emoji": emoji, "requests": int(req), "stars": int(stars)}


def invalidate_shop_tiers_cache() -> None:
    _purchase_tiers_cached.cache_clear()
    _gift_tiers_cached.cache_clear()
    _gift_map_cached.cache_clear()


async def gift_display_name(user_id: int, gift: dict[str, Any]) -> str:
    code = str(gift.get("code") or "").strip()
    base = (str(gift.get("title") or "").strip() or code or "Gift").strip()

    if code:
        with suppress(Exception):
            l10n = await t(user_id, f"gifts.{code}.title")
            if l10n:
                return str(l10n).strip()

    return base
