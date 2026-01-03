#app/services/addons/analytics.py
from __future__ import annotations

import asyncio
import html
import math
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Tuple, List

from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest, TelegramForbiddenError
from app.config import settings
from app.clients.telegram_client import get_bot
from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from app.core.memory import get_redis

RETENTION_DAYS = int(getattr(settings, "ANALYTICS_RETENTION_DAYS", 21))
ENABLE_ANALYTICS = bool(getattr(settings, "ANALYTICS_ENABLED", True))
USE_LLM_INSIGHTS = bool(getattr(settings, "ANALYTICS_USE_LLM", False))
ADMIN_IDS = list(getattr(settings, "ANALYTICS_REPORT_ADMIN_IDS", [])) or list(getattr(settings, "MODERATOR_IDS", []))
ALLOWED_CHATS = list(getattr(settings, "ALLOWED_GROUP_IDS", []) or [])

LAT_BUCKETS = [1000, 3000, 7000, 15000, 30000, 60000, 120000]

def _today_utc() -> datetime:
    return datetime.now(timezone.utc)

def _yesterday_utc_date() -> datetime.date:
    return (_today_utc() - timedelta(days=1)).date()

def _date_str_utc(d: datetime.date) -> str:
    return d.strftime("%Y%m%d")

def _human_date(d: datetime.date) -> str:
    return d.strftime("%Y-%m-%d")

def _k(chat_id: int, date_str: str, name: str) -> str:
    return f"an:{chat_id}:{date_str}:{name}"

def _expire_sec() -> int:
    return RETENTION_DAYS * 86400

def _b2s(x) -> str:
    if x is None:
        return ""
    if isinstance(x, (bytes, bytearray)):
        try:
            return x.decode("utf-8", "ignore")
        except Exception:
            return ""
    return str(x)

def _norm_reason(s: str) -> str:
    s = (s or "").strip()
    return (s or "unknown").lower().replace(" ", "_")[:48]

def _fmt_seconds(ms: Optional[int | float]) -> str:
    if ms is None:
        return "—"
    try:
        s = float(ms) / 1000.0
    except Exception:
        return "—"
    if s < 60:
        return f"{s:.1f}s"
    total = int(round(s))
    m, ss = divmod(total, 60)
    return f"{m}m {ss}s"

async def _safe_send_dm(chat_id: int, html_text: str) -> None:
    if len(html_text) > 3800:
        html_text = html_text[:3750] + "\n… (truncated)"
    bot = get_bot()
    attempt = 1
    MAX_ATTEMPTS = 5
    while attempt <= MAX_ATTEMPTS:
        try:
            await bot.send_message(chat_id, html_text, parse_mode="HTML", disable_web_page_preview=True)
            return
        except TelegramRetryAfter as e:
            delay = max(1, int(getattr(e, "retry_after", 5)))
            await asyncio.sleep(delay); attempt += 1
        except (TelegramBadRequest, TelegramForbiddenError):
            return
        except Exception:
            if attempt >= MAX_ATTEMPTS:
                return
            await asyncio.sleep(1.5 * attempt); attempt += 1

def _bucket_for_latency(ms: float) -> str:
    for edge in LAT_BUCKETS:
        if ms <= edge:
            return f"le_{edge}"
    return "gt"

async def record_user_message(
    chat_id: int,
    user_id: int,
    *,
    display_name: Optional[str] = None,
    content_type: str = "text",
    addressed_to_bot: bool = False,
    has_link: bool = False,
) -> None:
    if not ENABLE_ANALYTICS:
        return
    redis = get_redis()
    d = _date_str_utc(_today_utc().date())
    async with redis.pipeline(transaction=True) as p:
        p.incr(_k(chat_id, d, "msg_total"))
        p.incr(_k(chat_id, d, f"msg_type:{content_type}"))
        if addressed_to_bot:
            p.incr(_k(chat_id, d, "addressed_to_bot"))
        if has_link:
            p.incr(_k(chat_id, d, "link_msgs"))
        p.hincrby(_k(chat_id, d, "user_msg_counts"), str(user_id), 1)
        if display_name:
            p.hset(_k(chat_id, "meta", "usernames"), str(user_id), display_name[:128])
        p.pfadd(_k(chat_id, d, "hll_active_users"), str(user_id))
        p.expire(_k(chat_id, d, "msg_total"), _expire_sec())
        p.expire(_k(chat_id, d, f"msg_type:{content_type}"), _expire_sec())
        p.expire(_k(chat_id, d, "addressed_to_bot"), _expire_sec())
        p.expire(_k(chat_id, d, "link_msgs"), _expire_sec())
        p.expire(_k(chat_id, d, "user_msg_counts"), _expire_sec())
        p.expire(_k(chat_id, d, "hll_active_users"), _expire_sec())
        p.expire(_k(chat_id, "meta", "usernames"), 86400 * 120)
        await p.execute()

async def record_assistant_reply(chat_id: int, trigger: Optional[str]) -> None:
    if not ENABLE_ANALYTICS:
        return
    redis = get_redis()
    d = _date_str_utc(_today_utc().date())
    field = (trigger or "unknown").lower()
    async with redis.pipeline(transaction=True) as p:
        p.incr(_k(chat_id, d, "assistant_total"))
        p.hincrby(_k(chat_id, d, "assistant_by_trigger"), field, 1)
        p.expire(_k(chat_id, d, "assistant_total"), _expire_sec())
        p.expire(_k(chat_id, d, "assistant_by_trigger"), _expire_sec())
        await p.execute()

async def record_latency(chat_id: int, latency_ms: float) -> None:
    if not ENABLE_ANALYTICS:
        return
    redis = get_redis()
    d = _date_str_utc(_today_utc().date())
    bucket = _bucket_for_latency(latency_ms)
    async with redis.pipeline(transaction=True) as p:
        p.incrbyfloat(_k(chat_id, d, "lat_sum_ms"), float(latency_ms))
        p.incr(_k(chat_id, d, "lat_count"))
        p.hincrby(_k(chat_id, d, "lat_buckets"), bucket, 1)
        p.expire(_k(chat_id, d, "lat_sum_ms"), _expire_sec())
        p.expire(_k(chat_id, d, "lat_count"), _expire_sec())
        p.expire(_k(chat_id, d, "lat_buckets"), _expire_sec())
        await p.execute()

async def record_context(chat_id: int, *, checked: bool, on_topic: Optional[bool] = None, suppressed: bool = False) -> None:
    if not ENABLE_ANALYTICS:
        return
    redis = get_redis()
    d = _date_str_utc(_today_utc().date())
    async with redis.pipeline(transaction=True) as p:
        if checked:
            p.incr(_k(chat_id, d, "on_topic_checked"))
        if on_topic is True:
            p.incr(_k(chat_id, d, "on_topic_ok"))
        if suppressed:
            p.incr(_k(chat_id, d, "on_topic_suppressed"))
        p.expire(_k(chat_id, d, "on_topic_checked"), _expire_sec())
        p.expire(_k(chat_id, d, "on_topic_ok"), _expire_sec())
        p.expire(_k(chat_id, d, "on_topic_suppressed"), _expire_sec())
        await p.execute()

async def record_moderation(chat_id: int, status: str, reason: str) -> None:
    if not ENABLE_ANALYTICS:
        return
    redis = get_redis()
    d = _date_str_utc(_today_utc().date())
    r = _norm_reason(reason)
    async with redis.pipeline(transaction=True) as p:
        p.incr(_k(chat_id, d, "mod_total"))
        p.hincrby(_k(chat_id, d, "mod_by_status"), status or "unknown", 1)
        if r:
            p.hincrby(_k(chat_id, d, "mod_reasons"), r, 1)
        p.expire(_k(chat_id, d, "mod_total"), _expire_sec())
        p.expire(_k(chat_id, d, "mod_by_status"), _expire_sec())
        p.expire(_k(chat_id, d, "mod_reasons"), _expire_sec())
        await p.execute()

async def record_new_user(chat_id: int, user_id: int) -> None:
    if not ENABLE_ANALYTICS:
        return
    redis = get_redis()
    d = _date_str_utc(_today_utc().date())
    async with redis.pipeline(transaction=True) as p:
        p.incr(_k(chat_id, d, "new_users_total"))
        p.pfadd(_k(chat_id, d, "hll_new_users"), str(user_id))
        p.expire(_k(chat_id, d, "new_users_total"), _expire_sec())
        p.expire(_k(chat_id, d, "hll_new_users"), _expire_sec())
        await p.execute()

async def record_ping_sent(chat_id: int, kind: str) -> None:
    if not ENABLE_ANALYTICS:
        return
    redis = get_redis()
    d = _date_str_utc(_today_utc().date())
    async with redis.pipeline(transaction=True) as p:
        p.hincrby(_k(chat_id, d, "ping_sent"), (kind or "unknown").lower(), 1)
        p.expire(_k(chat_id, d, "ping_sent"), _expire_sec())
        await p.execute()

async def record_ping_response(chat_id: int, kind: str) -> None:
    if not ENABLE_ANALYTICS:
        return
    redis = get_redis()
    d = _date_str_utc(_today_utc().date())
    async with redis.pipeline(transaction=True) as p:
        p.hincrby(_k(chat_id, d, "ping_resp"), (kind or "unknown").lower(), 1)
        p.expire(_k(chat_id, d, "ping_resp"), _expire_sec())
        await p.execute()

async def record_timeout(chat_id: int) -> None:
    if not ENABLE_ANALYTICS:
        return
    redis = get_redis()
    d = _date_str_utc(_today_utc().date())
    async with redis.pipeline(transaction=True) as p:
        p.incr(_k(chat_id, d, "timeouts"))
        p.expire(_k(chat_id, d, "timeouts"), _expire_sec())
        await p.execute()

def _approx_quantiles_from_buckets(total: int, buckets: Dict[str, int]) -> Tuple[Optional[int], Optional[int]]:
    if total <= 0:
        return (None, None)
    edges = [f"le_{e}" for e in LAT_BUCKETS] + ["gt"]
    counts = [int(buckets.get(e, 0)) for e in edges]
    def _quantile(q: float) -> Optional[int]:
        target = math.ceil(total * q)
        acc = 0
        for i, c in enumerate(counts):
            acc += c
            if acc >= target:
                if edges[i] == "gt":
                    return None
                try:
                    return int(edges[i].split("_")[1])
                except Exception:
                    return None
        return None
    return _quantile(0.50), _quantile(0.95)

async def _load_day_snapshot(chat_id: int, date_obj: datetime.date) -> Dict:
    redis = get_redis()
    d = _date_str_utc(date_obj)
    out: Dict = {"date": _human_date(date_obj)}
    keys = [
        "msg_total","addressed_to_bot","link_msgs",
        "assistant_total","on_topic_checked","on_topic_ok","on_topic_suppressed",
        "lat_sum_ms","lat_count","timeouts","new_users_total","mod_total"
    ]
    res = await redis.mget(*[_k(chat_id, d, k) for k in keys])
    for k, v in zip(keys, res):
        s = _b2s(v)
        if k == "lat_sum_ms":
            out[k] = float(s) if s else 0.0
        else:
            out[k] = int(float(s)) if s else 0

    mt = await redis.mget(*[_k(chat_id, d, f"msg_type:{t}") for t in ("text","photo","voice","document")])
    out["msg_types"] = dict(zip(("text","photo","voice","document"), [int(float(_b2s(x) or 0)) for x in mt]))

    try:
        out["active_users"] = int(await redis.pfcount(_k(chat_id, d, "hll_active_users")))
    except Exception:
        out["active_users"] = 0

    try:
        out["new_users_unique"] = int(await redis.pfcount(_k(chat_id, d, "hll_new_users")))
    except Exception:
        out["new_users_unique"] = out.get("new_users_total", 0)

    abt = await redis.hgetall(_k(chat_id, d, "assistant_by_trigger"))
    out["assistant_by_trigger"] = { _b2s(k): int(_b2s(v) or 0) for k, v in (abt or {}).items() }

    lbs = await redis.hgetall(_k(chat_id, d, "lat_buckets"))
    buckets = { _b2s(k): int(_b2s(v) or 0) for k, v in (lbs or {}).items() }
    out["lat_p50"], out["lat_p95"] = (None, None)
    if out.get("lat_count", 0) > 0:
        out["lat_avg_ms"] = int(round((out.get("lat_sum_ms", 0.0) / max(1, out["lat_count"]))))
        p50, p95 = _approx_quantiles_from_buckets(out["lat_count"], buckets)
        out["lat_p50"] = p50
        out["lat_p95"] = p95
    else:
        out["lat_avg_ms"] = None

    mstat = await redis.hgetall(_k(chat_id, d, "mod_by_status"))
    out["mod_by_status"] = { _b2s(k): int(_b2s(v) or 0) for k, v in (mstat or {}).items() }
    mrs = await redis.hgetall(_k(chat_id, d, "mod_reasons"))
    out["mod_reasons"] = { _b2s(k): int(_b2s(v) or 0) for k, v in (mrs or {}).items() }

    prs = await redis.hgetall(_k(chat_id, d, "ping_resp"))
    pss = await redis.hgetall(_k(chat_id, d, "ping_sent"))
    out["ping_resp"] = { _b2s(k): int(_b2s(v) or 0) for k, v in (prs or {}).items() }
    out["ping_sent"] = { _b2s(k): int(_b2s(v) or 0) for k, v in (pss or {}).items() }

    raw = await redis.hgetall(_k(chat_id, d, "user_msg_counts"))
    _users_raw = await redis.hgetall(_k(chat_id, "meta", "usernames"))
    users_map = { _b2s(k): _b2s(v) for k, v in (_users_raw or {}).items() }
    pairs = []
    for uid, cnt in (raw or {}).items():
        try:
            pairs.append((int(_b2s(uid)), int(_b2s(cnt) or 0)))
        except Exception:
            continue
    pairs.sort(key=lambda kv: kv[1], reverse=True)
    top_n = []
    for uid, cnt in pairs[:10]:
        name = users_map.get(str(uid), "")
        top_n.append((uid, name, cnt))
    out["top_users"] = top_n
    return out

def _fmt_kv_label(key: str, val: Optional[int | float]) -> str:
    if val is None:
        return f"<b>{html.escape(key)}:</b> —"
    if isinstance(val, float) and val.is_integer():
        val = int(val)
    return f"<b>{html.escape(key)}:</b> {val}"

def _render_html(chat_id: int, date_human: str, snap: Dict) -> str:
    lines: List[str] = []

    lines.append(f"📊 <b>Daily Chat Report</b> ▫️ {html.escape(date_human)} UTC")
    lines.append(f"<i>Chat ID:</i> <code>{chat_id}</code>")
    lines.append("")

    kpi = [
        f"<b>Msgs</b> {snap.get('msg_total', 0)}",
        f"<b>Active</b> {snap.get('active_users', 0)}",
        f"<b>AI</b> {snap.get('assistant_total', 0)}",
        f"<b>New</b> {snap.get('new_users_unique', 0)}",
    ]
    lines.append("▫️ " + "  |  ".join(kpi))

    if snap.get("lat_avg_ms") is not None:
        lines.append(
            f"▫️ <b>Assistant avg response time:</b> {_fmt_seconds(snap['lat_avg_ms'])}"
        )

    lines.append("")
    lines.append("👥 <b>Social & Engagement</b>")
    lines.append(f"▫️ <b>Addressed to persona:</b> {snap.get('addressed_to_bot', 0)}")
    if snap.get("link_msgs"):
        lines.append(f"▫️ <b>Messages with links:</b> {snap.get('link_msgs', 0)}")
    lines.append(f"▫️ <b>New members:</b> {snap.get('new_users_total', 0)} (unique: {snap.get('new_users_unique', 0)})")

    checked = snap.get("on_topic_checked", 0)
    ok = snap.get("on_topic_ok", 0)
    supp = snap.get("on_topic_suppressed", 0)
    rate = f"{(ok / checked * 100):.1f}%" if checked else "—"
    lines.append("")
    lines.append("🎯 <b>On-topic filter</b>")
    lines.append(
        "▫️ "
        f"<b>Messages checked:</b> {checked}  |  "
        f"<b>On-topic used as context:</b> {ok} ({rate})  |  "
        f"<b>Filtered as off-topic/low-signal:</b> {supp}"
    )

    abt = snap.get("assistant_by_trigger", {})
    if abt:
        parts = [f"{html.escape(k)}:{v}" for k, v in sorted(abt.items(), key=lambda x: x[0])]
        lines.append("▫️ <b>Assistant triggers (by source):</b> " + ", ".join(parts))

    pr = snap.get("ping_resp", {})
    ps = snap.get("ping_sent", {})
    if pr or ps:
        lines.append("")
        lines.append("🔔 <b>Pings</b>")
        if ps:
            lines.append("▫️ Sent: " + ", ".join(f"{html.escape(k)} {v}" for k, v in ps.items()))
        if pr:
            lines.append("▫️ Responses: " + ", ".join(f"{html.escape(k)} {v}" for k, v in pr.items()))

    if snap.get("mod_total", 0) or snap.get("mod_by_status") or snap.get("mod_reasons"):
        lines.append("")
        lines.append("🛡️ <b>Safety & moderation</b>")
        lines.append(
            f"▫️ <b>Messages checked:</b> {snap.get('mod_total', 0)}"
        )

        mb = snap.get("mod_by_status", {})
        if mb:
            lines.append(
                "▫️ <b>By status:</b> "
                + ", ".join(f"{html.escape(k)}:{v}" for k, v in mb.items())
            )

        mr = snap.get("mod_reasons", {})
        if mr:
            top_rs = sorted(mr.items(), key=lambda kv: kv[1], reverse=True)[:5]
            lines.append(
                "▫️ <b>Top reasons:</b> "
                + ", ".join(f"{html.escape(k)}:{v}" for k, v in top_rs)
                + " (labels are internal categories)"
            )

    top_users = snap.get("top_users") or []
    if top_users:
        lines.append("")
        lines.append("🏅 <b>Top contributors</b>")
        for uid, name, cnt in top_users[:10]:
            label = (html.escape(name) if name else f"User {uid}")
            lines.append(f"▫️ {label}: {cnt}")

    return "\n".join(lines)

async def _llm_insights(markdown_metrics: str) -> Optional[str]:
    if not USE_LLM_INSIGHTS:
        return None
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.REASONING_MODEL,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You are a community activity analyst. "
                            "Focus on engagement, on-topic relevance, and safety. "
                            "Treat response time as informational only and do NOT flag it as an issue "
                            "unless avg response time exceeds 60000 ms or there are model timeouts."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Given the daily chat metrics below, produce:\n"
                            "1) Up to 3 insights (what changed, why it matters).\n"
                            "2) Up to 3 next actions (owner → action).\n"
                            "Keep it under 500 characters, bullet points, no fluff.\n"
                            "Do not mention latency if it is within normal range and no timeouts are reported.\n\n"
                            f"Metrics:\n{markdown_metrics}"
                        ),
                    },
                ],
                max_output_tokens=400,
                temperature=0.3,
            ),
            timeout=settings.REASONING_MODEL_TIMEOUT,
        )
        text = (_get_output_text(resp) or "").strip()
        return text[:1500]
    except Exception:
        return None

async def generate_and_send_report_for_chat(chat_id: int, date_obj: datetime.date, admin_ids: List[int]) -> None:
    snap = await _load_day_snapshot(chat_id, date_obj)
    html_body = _render_html(chat_id, snap["date"], snap)

    metrics_text = (
        f"date={snap['date']}, "
        f"msgs={snap.get('msg_total', 0)}, "
        f"active={snap.get('active_users', 0)}, "
        f"assistant={snap.get('assistant_total', 0)}, "
        f"on_topic={{checked:{snap.get('on_topic_checked', 0)}, "
        f"ok:{snap.get('on_topic_ok', 0)}, "
        f"suppressed:{snap.get('on_topic_suppressed', 0)}}}, "
        f"lat_avg_ms={snap.get('lat_avg_ms')}, "
        f"timeouts={snap.get('timeouts', 0)}, "
        f"new_users={snap.get('new_users_total', 0)}, "
        f"moderation={snap.get('mod_total', 0)}"
    )
    insight = await _llm_insights(metrics_text)
    if insight:
        html_body += "\n\n<b>Insights</b>\n" + html.escape(insight)

    redis = get_redis()
    date_str = _date_str_utc(date_obj)
    for admin in admin_ids:
        sent_key = _k(chat_id, date_str, f"report_sent:{admin}")
        if await redis.set(sent_key, "1", nx=True, ex=_expire_sec()):
            await _safe_send_dm(int(admin), html_body)

async def generate_and_send_daily_reports() -> None:
    if not ENABLE_ANALYTICS:
        return
    date_obj = _yesterday_utc_date()
    admins = [int(a) for a in ADMIN_IDS if a]
    if not admins:
        return
    if not ALLOWED_CHATS:
        return
    tasks = [generate_and_send_report_for_chat(int(cid), date_obj, admins) for cid in ALLOWED_CHATS]
    await asyncio.gather(*tasks, return_exceptions=True)