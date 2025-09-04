#app/services/addons/price_fetcher.py

import asyncio
import json
import logging
from typing import List

from app.clients.http_client import http_client

logger = logging.getLogger(__name__)

SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","ADAUSDT","XRPUSDT","NEARUSDT","TONUSDT"]

def format_price(price: float) -> str:
    if price >= 1000:
        decimals = 2
    elif price >= 1:
        decimals = 3
    else:
        decimals = 4
    return f"{price:,.{decimals}f}"

async def price_fetcher() -> List[str]:

    url = "https://api.binance.com/api/v3/ticker/24hr"
    params = {"symbols": json.dumps(SYMBOLS, separators=(",", ":"))}

    try:
        data = await asyncio.wait_for(
            http_client.get_json(url, params=params),
            timeout=8
        )
    except Exception:
        logger.exception("price_fetcher: request failed")
        return ["💹 Crypto Market 💹\n_____________________________\n<i>Failed to fetch data.</i>"]

    if not isinstance(data, list):
        logger.warning("price_fetcher: unexpected payload type: %r", type(data))
        return ["💹 Crypto Market 💹\n_____________________________\n<i>No data available.</i>"]

    by_symbol = {it.get("symbol"): it for it in data if isinstance(it, dict)}

    lines = ["💹 Crypto Market 💹", "_____________________________"]
    for sym in SYMBOLS:
        it = by_symbol.get(sym)
        if not it:
            continue
        try:
            price = float(it.get("lastPrice"))
        except (TypeError, ValueError):
            continue
        try:
            change_pct = float(it.get("priceChangePercent", 0.0))
        except (TypeError, ValueError):
            change_pct = 0.0
        lines.append(f"{sym} - $ {format_price(price)} ({change_pct:+.2f}%)")

    if len(lines) <= 2:
        return ["💹 Crypto Market 💹\n_____________________________\n<i>No data available.</i>"]

    return ["\n".join(lines)]

