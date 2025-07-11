#app/services/news_fetcher.py

import asyncio
import logging
import json
from datetime import datetime
from typing import List

from app.clients.http_client import http_client

logger = logging.getLogger(__name__)

def format_price(price: float) -> str:

    if price >= 1000:
        decimals = 2
    elif price >= 1:
        decimals = 3
    else:
        decimals = 4
    return f"{price:,.{decimals}f}"

async def price_fetcher() -> List[str]:
    stats_url = "https://api.binance.com/api/v3/ticker/24hr"
    price_url = "https://api.binance.com/api/v3/ticker/price"
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "XRPUSDT", "NEARUSDT", "TONUSDT"]
    params = {"symbols": json.dumps(symbols, separators=(",", ":"))}

    stats_task = http_client.get_json(stats_url, params=params)
    prices_task = http_client.get_json(price_url, params=params)

    try:
        stats_data, price_data = await asyncio.gather(stats_task, prices_task)
    except Exception:
        logger.exception("Error fetching crypto data")
        return ["💹 Crypto Market 💹\n_____________________________\n<i>Failed to fetch data.</i>"]

    if not isinstance(stats_data, list) or not isinstance(price_data, list):
        logger.warning("Unexpected response format for symbols: %r", symbols)
        return ["💹 Crypto Market 💹\n_____________________________\n<i>No data available.</i>"]

    price_map = {item["symbol"]: float(item["price"]) for item in price_data}

    lines: List[str] = [
        "💹 Crypto Market 💹",
        "_____________________________",
    ]

    for stat in stats_data:
        symbol = stat.get("symbol")
        if symbol not in price_map:
            continue

        current_price = price_map[symbol]
        change_percent = float(stat["priceChangePercent"])

        price_str = format_price(current_price)
        change_str = f"{change_percent:+.2f}%"

        lines.append(f"{symbol} - $ {price_str} ({change_str})")

    message = "\n".join(lines)
    logger.debug("Formatted crypto message:\n%s", message)
    return [message]
