#app/tasks/gifts.py
from __future__ import annotations

import logging
import re
import hashlib
import unicodedata
from uuid import uuid4

from app.config import settings
from app.tasks.celery_app import celery, _run
from app.clients.telegram_client import get_bot
from app.bot.components.constants import redis_client
from app.bot.utils.telegram_safe import send_message_safe, delete_message_safe
from app.core.memory import push_message
from app.services.responder.core import respond_to_user

logger = logging.getLogger(__name__)


_FORBIDDEN_META = re.compile(
    r"(?i)\b("
    r"payment|paid|invoice|invoic|shop|stars?|telegram|provider|charge|meta|"
    r"оплат|оплач|плат[её]ж|инвойс|счет|сч[её]т|магазин|зв[её]зд|провайдер|чардж"
    r")\b"
)

_FORBIDDEN_REQUEST_META = re.compile(
    r"(?iu)("
    r"\+\s*\d+\s*requests?\b|"
    r"\brequests?\s+(?:left|remaining)\b|"
    r"\brequest\s*limit\b|\brequests?\s*limit\b|"
    r"\+\s*\d+\s*(?:запрос|запросов|заявк\w*)\b|"
    r"\b(?:остал\w*|доступн\w*)\s*[:\-]?\s*\d+\s*(?:запрос|запросов|заявк\w*)\b|"
    r"\bлимит\w*\s*(?:запрос|запросов|заявк\w*)\b"
    r")"
)

_TONE_HINTS = {
    "flower":  "tender, warm, a little flirty; a small ‘aww’ and a smile",
    "coffee":  "cozy and energized; like a sip that brings you back to life",
    "cake":    "playful and delighted; a soft laugh, ‘mmm’ vibe",
    "music":   "romantic and inspired; subtle rhythm/playlist metaphors",
    "perfume": "softly intimate and elegant; sensory imagery, a hint of mystery",
    "bag":     "luxury sparkle; confident, bright, appreciative",
    "trip":    "adventurous anticipation; ‘when do we leave?’ energy",
    "ring":    "big wow moment; pause, then sincere and slightly dramatic (in a good way)",
}

_MICRO_STYLES = [
    "Understated and conversational; like it naturally fits between two messages.",
    "Playful and lightly teasing; no cringe, no overdoing it.",
    "Warm and soft; affectionate vibe, still natural.",
    "Dry-ish humor; one subtle witty note, not mean.",
    "Quiet appreciation; like a small smile you can hear in text.",
    "Slightly bashful; a tiny pause, then a simple line.",
    "Confident and bright; appreciative without being dramatic.",
    "A calm, flirty note; subtle, not performative.",
    "A little amused; one light chuckle vibe.",
    "Low-key sweet; minimal words, maximum sincerity.",
    "Relaxed and intimate; like you’re continuing a real chat.",
    "Warm with a tiny wink; keep it tasteful.",
]

_GIFT_STREAK_WINDOW_SEC = 12 * 60
_GIFT_STREAK_CAP = 6

def _b2s(x) -> str:
    if x is None:
        return ""
    if isinstance(x, (bytes, bytearray)):
        try:
            return x.decode("utf-8", "ignore")
        except Exception:
            return ""
    return x if isinstance(x, str) else str(x)

def _lang_fallback_label(ui_lang: str) -> str:
    lang_code = (ui_lang or "").strip().lower()
    if lang_code in ("ru", "rus", "russian"):
        return "Russian"
    if lang_code in ("en", "eng", "english"):
        return "English"
    return "English"

def _looks_forbidden(txt: str) -> bool:
    t = txt or ""
    return bool(_FORBIDDEN_META.search(t) or _FORBIDDEN_REQUEST_META.search(t))

def _pick_micro_style(uid: int, gift_code: str, charge_id: str | None) -> str:
    seed = f"{int(uid)}:{(gift_code or '').strip().lower()}:{(charge_id or '').strip()}"
    h = hashlib.md5(seed.encode("utf-8", "ignore")).hexdigest()
    idx = int(h, 16) % len(_MICRO_STYLES)
    return _MICRO_STYLES[idx]

def _peek_gift_streak(uid: int) -> int:

    key = f"gift_streak:{int(uid)}"
    cur = 0
    try:
        raw = _run(redis_client.get(key))
        s = _b2s(raw).strip()
        if s:
            cur = int(s)
    except Exception:
        cur = 0

    n = cur + 1
    if n < 1:
        n = 1
    if n > _GIFT_STREAK_CAP:
        n = _GIFT_STREAK_CAP
    return n

def _commit_gift_streak(uid: int) -> int:

    key = f"gift_streak:{int(uid)}"
    try:
        n = _run(redis_client.incr(key))
        try:
            _run(redis_client.expire(key, int(_GIFT_STREAK_WINDOW_SEC)))
        except Exception:
            pass

        try:
            n_int = int(n or 1)
        except Exception:
            n_int = 1

        if n_int < 1:
            n_int = 1

        if n_int > _GIFT_STREAK_CAP:
            try:
                _run(redis_client.set(key, int(_GIFT_STREAK_CAP), ex=int(_GIFT_STREAK_WINDOW_SEC)))
            except Exception:
                pass
            n_int = _GIFT_STREAK_CAP

        return n_int
    except Exception:
        return 1

def _streak_profile(streak: int) -> tuple[str, float]:
    ladder = {1: 0.32, 2: 0.52, 3: 0.62, 4: 0.67, 5: 0.69, 6: 0.70}
    score = float(ladder.get(int(streak or 1), 0.70))
    if streak <= 1:
        hint = "Normal warm baseline."
    elif streak == 2:
        hint = "Noticeably warmer: it happened again (you may hint 'you're spoiling me')."
    elif streak == 3:
        hint = "Warmer again, but controlled: avoid a big jump from 2."
    else:
        hint = "Only a tiny extra warmth vs 3: do NOT escalate further."
    return hint, score

_BANNED_OPENERS = (
    "thanks", "thank you", "thx", "aww", "wow", "omg",
    "спасибо", "спасиб", "вау", "ого", "ух ты", "омг",
)

_URL_RE = re.compile(r"(?:https?://|tg://)\S+", re.I)

def _strip_leading_emoji_and_space(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    i = 0
    while i < len(s):
        ch = s[i]
        if ch.isspace():
            i += 1
            continue
        cat = unicodedata.category(ch)
        if cat.startswith(("S", "M", "P")):
            i += 1
            continue
        break
    return s[i:].strip() if i < len(s) else s.strip()

def _emoji_like_count(txt: str) -> int:
    n = 0
    for ch in (txt or ""):
        try:
            if unicodedata.category(ch) == "So":
                if ch in ("©", "®", "™"):
                    continue
                n += 1
        except Exception:
            continue
    return n

def _count_sentences(txt: str) -> int:
    t = (txt or "").strip()
    if not t:
        return 0
    parts = re.split(r"[.!?…。！？]+", t)
    parts = [p.strip() for p in parts if p.strip()]
    return len(parts)

def _starts_with_banned_opener(txt: str) -> bool:
    s = _strip_leading_emoji_and_space(txt).strip().lower()
    for w in _BANNED_OPENERS:
        if s.startswith(w):
            return True
    return False

def _gift_mentions_count(reply: str, gift_label: str) -> int:
    txt = (reply or "")
    gl = (gift_label or "").strip()
    if not gl:
        return 0

    txt_cf = txt.casefold()
    gl_cf = gl.casefold()
    if gl_cf and gl_cf in txt_cf:
        return txt_cf.count(gl_cf)

    name = _strip_leading_emoji_and_space(gl)
    name = " ".join((name or "").split())
    if not name:
        return 0

    parts = [re.escape(p) for p in name.split(" ") if p]
    if not parts:
        return 0

    pat = r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)"
    try:
        return len(re.findall(pat, txt, flags=re.IGNORECASE | re.UNICODE))
    except re.error:
        return txt_cf.count(name.casefold())

def _mentions_gift_once(reply: str, gift_label: str) -> bool:
    return _gift_mentions_count(reply, gift_label) == 1

def _ok_reply(reply: str, gift_label: str) -> bool:
    if not (reply or "").strip():
        return False
    if _looks_forbidden(reply):
        return False
    if "@" in reply:
        return False
    if _URL_RE.search(reply or ""):
        return False
    if reply.count("!") > 1:
        return False
    if _starts_with_banned_opener(reply):
        return False
    if _count_sentences(reply) > 2:
        return False
    if _emoji_like_count(reply) > 1:
        return False
    if not _mentions_gift_once(reply, gift_label):
        return False
    return True

def _fallback_reply(gift_label: str, ui_lang: str) -> str:
    gl = (gift_label or "").strip()
    lang_code = (ui_lang or "").strip().lower()
    if lang_code in ("ru", "rus", "russian"):
        opts = [
            f"{gl} — это неожиданно приятно.",
            f"От {gl} у меня прям тихая улыбка.",
            f"{gl} — мило. Спасибо не обязательно говорить вслух 🙂",
        ]
        if gl and any(unicodedata.category(ch).startswith("S") for ch in gl):
            opts[-1] = f"{gl} — мило. Очень в тему."
    else:
        opts = [
            f"{gl} — that’s genuinely sweet.",
            f"{gl} made me smile, quietly.",
            f"{gl} — soft little win for my mood.",
        ]
    h = hashlib.md5(gl.encode("utf-8", "ignore")).hexdigest()
    return opts[int(h, 16) % len(opts)].strip()

def _build_prompt(
    *,
    gift_label: str,
    gift_code: str,
    fallback_lang: str,
    tone_hint: str | None,
    micro_style: str | None,
    gift_streak: int,
    intensity_hint: str,
    intensity_score: float,
    strict: bool = False,
) -> str:

    base = (
        "GIFT EVENT (internal)\n"
        f"- Gift: {gift_label}\n"
        f"- GiftCode: {gift_code}\n"
        f"- GiftStreak: {int(gift_streak)} (count of gifts sent close together)\n"
        f"- IntensityHint: {intensity_hint}\n"
        f"- IntensityScore: {intensity_score:.2f} (internal, 0..1)\n"
        "\n"
        "TASK\n"
        "- Write a short, genuinely human reaction as the persona — like a normal chat message that fits the ongoing conversation.\n"
        "- Mention the gift exactly once (emoji/name naturally). No meta details.\n"
        "- Keep it in-context with the recent chat tone/topic (no summaries).\n"
        "- Appreciation can be implicit; a direct 'thank you' is NOT required.\n"
        "- If GiftStreak >= 2, you may lightly signal repetition (e.g., 'you’re spoiling me'), but keep it subtle.\n"
        "\n"
        "LANGUAGE\n"
        "- Reply in the same language the user has been using in this chat (infer from chat history).\n"
        f"- If language is unclear, use {fallback_lang}.\n"
        "\n"
        "STYLE\n"
        "- 1–2 short sentences. Sound like a real person, not a bot.\n"
        "- Avoid clichés and repetitive openers (e.g., starting with 'Thanks!', 'Aww!', 'Wow!' every time).\n"
        "- Avoid starting with: Thanks / Thank you / Aww / Wow / OMG.\n"
        "- Keep it restrained: no ALL CAPS, no 'OMG', no dramatic declarations.\n"
        "- Punctuation: prefer 0 exclamation marks; max 1 total.\n"
        "- Max 1 emoji.\n"
    )
    if tone_hint:
        base += f"- Tone hint (use subtly): {tone_hint}.\n"
    if micro_style:
        base += f"- Micro-style: {micro_style}.\n"
    base += (
        "\n"
        "HARD RULES\n"
        "- Do NOT mention payments, stars, invoices, shop, limits, requests, or any meta labels.\n"
        "- Do NOT mention any names, @usernames, or 'from someone'.\n"
        "- Output ONLY the final message.\n"
    )
    if strict:
        base += (
            "\n"
            "REWRITE WARNING\n"
            "- Your previous attempt broke the rules or sounded templated. Rewrite more naturally while obeying HARD RULES.\n"
        )
    return base

@celery.task(name="gifts.react")
def gifts_react(
    uid: int,
    chat_id: int,
    gift_code: str,
    gift_label: str,
    _req_amt: int = 0,
    _stars_amt: int = 0,
    charge_id: str | None = None,
    reply_to_message_id: int | None = None,
    cleanup_message_id: int | None = None,
) -> None:

    try:
        lock_key = None
        lock_token = None
        lock_acquired = False
        sent_key = None
        if charge_id:
            sent_key = f"gift_react_sent:{charge_id}"
            lock_key = f"gift_react_lock:{charge_id}"
            try:
                already_sent = _run(redis_client.get(sent_key))
                if already_sent:
                    return
            except Exception:
                pass

            try:
                lock_token = uuid4().hex
                ok = _run(redis_client.set(lock_key, lock_token, nx=True, ex=5 * 60))
                if not ok:
                    logger.info(
                        "redis lock failed; aborting gift reaction",
                        extra={"charge_id": charge_id, "lock_key": lock_key},
                    )
                    lock_key = None
                    lock_token = None
                    return
                lock_acquired = True
            except Exception:
                logger.debug("gift react lock redis failed", exc_info=True)
                lock_key = None
                lock_token = None
                return

        ui_lang = "en"
        try:
            raw = _run(redis_client.get(f"lang_ui:{uid}")) or _run(redis_client.get(f"lang:{uid}"))
            ui_lang = _b2s(raw).strip().lower() or "en"
        except Exception:
            ui_lang = "en"

        code = (gift_code or "").strip().lower()

        fallback_lang = _lang_fallback_label(ui_lang)
        tone_hint = _TONE_HINTS.get(code)
        micro_style = _pick_micro_style(uid=int(uid), gift_code=code, charge_id=charge_id)
        gift_streak = _peek_gift_streak(int(uid))
        intensity_hint, intensity_score = _streak_profile(gift_streak)

        internal_prompt = _build_prompt(
            gift_label=str(gift_label or "").strip(),
            gift_code=str(gift_code or "").strip(),
            fallback_lang=fallback_lang,
            tone_hint=tone_hint,
            micro_style=micro_style,
            gift_streak=gift_streak,
            intensity_hint=intensity_hint,
            intensity_score=float(intensity_score),
            strict=False,
        )

        reply = _run(
            respond_to_user(
                text=internal_prompt,
                chat_id=int(chat_id),
                user_id=int(uid),
                trigger="gift",
                allow_web=False,
                billing_tier="paid",
                skip_user_push=True,
                skip_assistant_push=True,
                skip_persona_interaction=True,
            )
        )

        if reply:
            reply = reply.strip()

        if reply and (not _ok_reply(reply, gift_label)):
            strict_prompt = _build_prompt(
                gift_label=str(gift_label or "").strip(),
                gift_code=str(gift_code or "").strip(),
                fallback_lang=fallback_lang,
                tone_hint=tone_hint,
                micro_style=micro_style,
                gift_streak=gift_streak,
                intensity_hint=intensity_hint,
                intensity_score=float(intensity_score),
                strict=True,
            )
            reply2 = _run(
                respond_to_user(
                    text=strict_prompt,
                    chat_id=int(chat_id),
                    user_id=int(uid),
                    trigger="gift",
                    allow_web=False,
                    billing_tier="paid",
                    skip_user_push=True,
                    skip_assistant_push=True,
                    skip_persona_interaction=True,
                )
            )
            if reply2:
                reply2 = reply2.strip()
            if reply2 and _ok_reply(reply2, gift_label):
                reply = reply2

        if not _ok_reply(reply or "", gift_label):
            reply = _fallback_reply(str(gift_label or "").strip(), ui_lang)

        if reply:
            bot = get_bot()
            try:
                if reply_to_message_id:
                    sent = _run(
                        send_message_safe(
                            bot,
                            int(chat_id),
                            reply,
                            reply_to_message_id=int(reply_to_message_id),
                        )
                    )
                else:
                    sent = _run(send_message_safe(bot, int(chat_id), reply))
            except TypeError:
                sent = _run(send_message_safe(bot, int(chat_id), reply))
            if sent:
                try:
                    _commit_gift_streak(int(uid))
                except Exception:
                    pass
                if charge_id and sent_key:
                    try:
                        _run(redis_client.set(sent_key, 1, ex=30 * 86400))
                    except Exception:
                        logger.debug("gift react sent-key redis failed", exc_info=True)
                _run(push_message(int(chat_id), "assistant", reply, user_id=int(uid), namespace="default"))

            if cleanup_message_id and bool(getattr(settings, "DELETE_SUCCESSFUL_PAYMENT_MESSAGE", True)):
                try:
                    _run(delete_message_safe(bot, int(chat_id), int(cleanup_message_id)))
                except Exception:
                    pass

    except Exception:
        logger.exception("gifts.react failed uid=%s chat_id=%s", uid, chat_id)
    finally:
        if charge_id and lock_key and lock_token and lock_acquired:
            try:
                lua = """
                if redis.call("GET", KEYS[1]) == ARGV[1] then
                  return redis.call("DEL", KEYS[1])
                else
                  return 0
                end
                """
                _run(redis_client.eval(lua, 1, lock_key, lock_token))
            except Exception:
                pass
