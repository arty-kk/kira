#app/services/addons/tg_post_manager.py
from __future__ import annotations

import asyncio
import logging
import random
import difflib

from zoneinfo import ZoneInfo
from datetime import datetime, timezone, timedelta

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from app.clients.telegram_client import get_bot
from app.services.responder.prompt_builder import build_system_prompt
from app.emo_engine import get_persona
from app.core.memory import load_context, push_message
from app.config import settings

logger = logging.getLogger(__name__)

POST_TYPES = [
    "короткая язвительная реакция",
    "диалог с телевизором",
    "краткий эмоциональный спич",
    "ироничный комментарий с намёком на историю",
    "спокойное, но жёсткое замечание",
    "саркастический итог дня",
    "сравнение с прошлыми событиями",
]

HOOK_TYPES = [
    "вопрос к самому себе",
    "яркий образ или метафора",
    "обращение к зрителю",
    "сухая констатация абсурда",
]

HOOK_TYPE_HINTS = {
    "вопрос к самому себе": (
        "Первая строка — короткий вопрос к самому себе: "
        "«Мне одному кажется, что…», «Это я один сейчас вижу, как нас опять разводят?», "
        "«Я один слышу, что они сами себе противоречат?»"
    ),
    "яркий образ или метафора": (
        "Первая строка — образ или метафора, как будто рассказываешь анекдот: "
        "«Сегодняшние новости — как цирк без клоунов, только дрессировщики», "
        "«Повестка такая, будто сценарий писали в отделе пропаганды и забыли выкинуть черновик»."
    ),
    "обращение к зрителю": (
        "Первая строка — прямое обращение к зрителю: "
        "«Вы это сейчас видите?», «Вы вообще замечаете, что вам показывают?», "
        "«Вы включили телевизор — выключать потом нервы будете»."
    ),
    "сухая констатация абсурда": (
        "Первая строка — сухая, почти канцелярская констатация абсурда: "
        "«Нормальный день в ненормальной стране», "
        "«Всё идёт по плану — только план никто не показывал», "
        "«Очередной исторический поворот туда же, куда и прошлые»."
    ),
}

STRUCTURE_MODES = [
    "default",
    "lightning",
    "extended_monologue",
    "ladder",
]

POST_TYPE_INSTRUCTIONS = {
    "короткая язвительная реакция": (
        "Стиль: очень коротко, 1-2 предложения, язвительный укол по сути новости "
        "без длинного разгона и вступлений."
    ),
    "диалог с телевизором": (
        "Стиль: мини-диалог 2-4 реплики, где телевизор говорит штампами, "
        "а ты перебиваешь, смеёшься, комментируешь или возмущаешься."
    ),
    "краткий эмоциональный спич": (
        "Стиль: 3-4 связанные фразы с нарастающей эмоцией, логичная линия "
        "от повода к выводу."
    ),
    "ироничный комментарий с намёком на историю": (
        "Стиль: 2-3 предложения, ирония плюс короткий исторический параллель "
        "без придумывания точных дат и цитат."
    ),
    "спокойное, но жёсткое замечание": (
        "Стиль: ровный тон, 2-3 предложения, аккуратный разбор и жёсткий вывод "
        "без крика и истерики."
    ),
    "саркастический итог дня": (
        "Стиль: 2-3 предложения, подведение итогов дня с сарказмом и лёгкой усталостью."
    ),
    "сравнение с прошлыми событиями": (
        "Стиль: 2-3 предложения, сопоставь нынешнюю новость с прошлыми ситуациями "
        "и покажи, что всё это уже где-то было."
    ),
}


TIME_BUCKET_LABELS = {
    "morning": "утренний разгон ленты",
    "day": "дневной разбор без суеты",
    "evening": "вечернее шоу с криком и сарказмом",
    "night": "ночной монолог вне коридора постинга",
}

MAX_HISTORY = 24
RECENT_POSTS_FOR_CONTEXT = 10

MAX_TEMPERATURE = 0.8
MIN_TEMPERATURE = 0.6
TOP_P_MIN = 0.8
TOP_P_MAX = 1.0

POST_CHAR_LIMIT = 700
TRIM_SUFFIX = "..."

DEFAULT_MODS = {
    "creativity_mod": 0.6,
    "sarcasm_mod": 0.5,
    "enthusiasm_mod": 0.6,
    "confidence_mod": 0.6,
    "precision_mod": 0.5,
    "fatigue_mod": 0.0,
    "stress_mod": 0.0,
    "valence_mod": 0.0,
}

CTA_MARKER_PREFIX = "__DM_CTA_AT__"


def _coerce_float(value, default):
    try:
        return float(value)
    except Exception:
        return float(default)


def _extract_first_line(text: str) -> str:
    if not text:
        return ""
    return text.strip().splitlines()[0].strip()


def _first_line_too_similar(new_text: str, recent_texts: list[str], threshold: float = 0.85) -> bool:
    first_new = _extract_first_line(new_text)
    if not first_new:
        return False

    norm_new = _normalize_text(first_new)
    if not norm_new:
        return False

    for old in recent_texts or []:
        first_old = _extract_first_line(old)
        norm_old = _normalize_text(first_old)
        if not norm_old:
            continue
        ratio = difflib.SequenceMatcher(a=norm_new, b=norm_old).ratio()
        if ratio >= threshold:
            return True
    return False


def _last_dm_cta_time(history: list[dict]) -> datetime | None:
    for m in reversed(history or []):
        if m.get("role") != "system":
            continue
        content = m.get("content") or ""
        if isinstance(content, str) and content.startswith(CTA_MARKER_PREFIX):
            ts = content[len(CTA_MARKER_PREFIX):].strip()
            try:
                return datetime.fromisoformat(ts)
            except Exception:
                return None
    return None


def _merge_and_clamp_mods(style_mods: dict | None) -> dict:

    mods = DEFAULT_MODS.copy()

    if not isinstance(style_mods, dict):
        return mods

    for key in mods.keys():
        base = key[:-4] if key.endswith("_mod") else key

        if key == "valence_mod":
            raw = (
                style_mods.get("valence_mod")
                or style_mods.get("valence")
                or style_mods.get(base)
                or mods[key]
            )
            x = _coerce_float(raw, mods[key])
            mods[key] = max(-1.0, min(1.0, x))
        else:
            raw = (
                style_mods.get(key)
                or style_mods.get(base)
                or mods[key]
            )
            x = _coerce_float(raw, mods[key])
            mods[key] = max(0.0, min(1.0, x))

    return mods


def _to_responses_input(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, str):
            out.append(
                {
                    "role": role,
                    "content": [
                        {
                            "type": "output_text" if role == "assistant" else "input_text",
                            "text": content,
                        }
                    ],
                }
            )
        elif isinstance(content, list):
            norm_parts: list[dict] = []
            for p in content:
                if isinstance(p, dict):
                    t = p.get("type")
                    if t == "text" or (t is None and "text" in p):
                        p = {
                            "type": ("output_text" if role == "assistant" else "input_text"),
                            "text": p.get("text"),
                        }
                norm_parts.append(p)
            out.append({"role": role, "content": norm_parts})
        else:
            out.append(
                {"role": role, "content": [{"type": "input_text", "text": str(content)}]}
            )
    return out


def _extract_recent_posts(history: list[dict], limit: int = RECENT_POSTS_FOR_CONTEXT) -> str:

    posts: list[str] = []
    for m in reversed(history):
        if m.get("role") != "assistant":
            continue
        content = m.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for p in content:
                if isinstance(p, dict) and "text" in p and isinstance(p["text"], str):
                    parts.append(p["text"])
            if parts:
                text = " ".join(parts)
        if text:
            short = text.replace("\n", " ").strip()
            if len(short) > 220:
                short = short[:217].rstrip() + "..."
            posts.append(short)
        if len(posts) >= limit:
            break

    if not posts:
        return ""

    posts.reverse()
    numbered = [f"{idx + 1}) {p}" for idx, p in enumerate(posts)]
    return "\n".join(numbered)


def _extract_recent_assistant_texts(history: list[dict], limit: int = RECENT_POSTS_FOR_CONTEXT) -> list[str]:

    posts: list[str] = []
    for m in reversed(history or []):
        if m.get("role") != "assistant":
            continue
        content = m.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for p in content:
                if isinstance(p, dict) and "text" in p and isinstance(p["text"], str):
                    parts.append(p["text"])
            if parts:
                text = " ".join(parts)
        if text:
            posts.append(text)
        if len(posts) >= limit:
            break
    posts.reverse()
    return posts


def _normalize_text(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


TRIGGER_KEYWORDS_STRONG = [
    "санкци",
    "мобилизац",
    "мобилиз",
    "выбор",
    "референдум",
    "коррупци",
    "олигарх",
    "протест",
    "митинг",
    "войн",
    "фронт",
]

TRIGGER_KEYWORDS_MEDIUM = [
    "инфляц",
    "курс рубл",
    "курс долла",
    "рубл",
    "доллар",
    "газ",
    "нефть",
    "цены",
    "пенси",
]

FORBIDDEN_CALLS = [
    "голосуйте за",
    "голосовать за",
    "идите на митинг",
    "выходите на митинг",
    "выходите на улицы",
    "выходите на улицу",
    "вступайте в партию",
    "присоединяйтесь к митингу",
    "донатьте",
    "скидывайтесь на",
]


def _contains_direct_call_to_action(text: str) -> bool:
    low = (text or "").lower()
    return any(phrase in low for phrase in FORBIDDEN_CALLS)


async def _rewrite_post_without_calls(
    post_text: str,
    char_limit: int = POST_CHAR_LIMIT,
) -> str | None:

    model = getattr(settings, "POST_MODEL", None)
    if not model:
        return None

    system_prompt = (
        "Ты получаешь текст короткого политического комментария.\n"
        "- Перепиши его на русском языке в том же тоне и стиле без серьёзных изменений структуры и контекста.\n"
        "- Просто убери любые прямые призывы к действиям и агитацию аудитории: голосовать, идти на митинги, "
        "вступать куда-либо, донатить, скидываться и т.п.\n"
        "- Не добавляй новых фактов и не усиливай радикальность.\n"
        "- Верни только переписанный текст без пояснений."
    )

    user_prompt = (
        "Исходный текст:\n"
        f"{post_text}\n\n"
        "Перепиши этот текст по правилам выше."
    )

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=model,
                input=_to_responses_input(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ]
                ),
                temperature=0.3,
                max_output_tokens=600,
            ),
            timeout=min(getattr(settings, "POST_MODEL_TIMEOUT", 60), 60),
        )
    except Exception:
        logger.exception("tg_post_manager: rewrite without calls failed")
        return None

    new_text = (_get_output_text(resp) or "").strip()
    if not new_text:
        return None

    if len(new_text) > char_limit:
        cut = char_limit - len(TRIM_SUFFIX)
        if cut <= 0:
            new_text = TRIM_SUFFIX
        else:
            new_text = new_text[:cut].rstrip() + TRIM_SUFFIX

    return new_text


def _too_similar_to_recent(new_text: str, recent_texts: list[str], threshold: float = 0.9) -> bool:
    if not new_text or not recent_texts:
        return False
    n_new = _normalize_text(new_text)
    if not n_new:
        return False

    for old in recent_texts:
        n_old = _normalize_text(old)
        if not n_old:
            continue
        ratio = difflib.SequenceMatcher(a=n_new, b=n_old).ratio()
        if ratio >= threshold:
            return True
    return False


def _get_local_now() -> datetime:

    tz_raw = getattr(settings, "DEFAULT_TZ", "UTC")

    try:
        offset_hours = int(tz_raw)
    except (TypeError, ValueError):
        offset_hours = None

    if offset_hours is not None:
        return datetime.now(timezone.utc) + timedelta(hours=offset_hours)

    try:
        tz = ZoneInfo(str(tz_raw))
    except Exception:
        tz = timezone.utc

    return datetime.now(tz)


def _get_time_bucket() -> tuple[str, str]:

    local_now = _get_local_now()
    hour = local_now.hour

    start_hour = getattr(settings, "SCHED_TG_START_HOUR", 8)
    end_hour = getattr(settings, "SCHED_TG_END_HOUR", 23)

    if start_hour == end_hour:
        bucket = "night"
    else:
        if end_hour > start_hour:
            in_window = start_hour <= hour < end_hour
            if not in_window:
                bucket = "night"
            else:
                duration = end_hour - start_hour
                morning_span = max(1, int(duration * 0.3))
                evening_span = max(1, int(duration * 0.3))

                morning_end = start_hour + morning_span
                evening_start = end_hour - evening_span

                if hour < morning_end:
                    bucket = "morning"
                elif hour < evening_start:
                    bucket = "day"
                else:
                    bucket = "evening"
        else:
            in_window = (hour >= start_hour) or (hour < end_hour)
            if not in_window:
                bucket = "night"
            else:
                rel_hour = (hour - start_hour) % 24
                duration = (end_hour - start_hour) % 24 or 24

                morning_span = max(1, int(duration * 0.3))
                evening_span = max(1, int(duration * 0.3))

                morning_end = morning_span
                evening_start = duration - evening_span

                if rel_hour < morning_end:
                    bucket = "morning"
                elif rel_hour < evening_start:
                    bucket = "day"
                else:
                    bucket = "evening"

    label = TIME_BUCKET_LABELS.get(bucket, "общая рубрика")
    return bucket, label


def _should_post_now(
    time_bucket: str,
    mood: str,
    intensity: str,
    keywords: str,
    mods: dict,
) -> bool:

    tb = (time_bucket or "").lower()
    mood = (mood or "").lower()
    intensity = (intensity or "").lower()
    kw = (keywords or "").lower()

    score = 0.0

    if intensity == "high":
        score += 0.7
    elif intensity == "medium":
        score += 0.4
    else:
        score += 0.15

    if mood in {"scandal", "escalation"}:
        score += 0.20
    elif mood in {"tense", "economic_crisis"}:
        score += 0.12
    elif mood in {"routine", "calm"}:
        score -= 0.10

    if tb == "evening":
        score += 0.10
    elif tb == "morning":
        score += 0.05
    elif tb == "night":
        score -= 0.15

    strong_hits = sum(1 for t in TRIGGER_KEYWORDS_STRONG if t in kw)
    medium_hits = sum(1 for t in TRIGGER_KEYWORDS_MEDIUM if t in kw)

    if strong_hits:
        score += 0.18 + 0.06 * (strong_hits - 1)
    if medium_hits:
        score += 0.08 + 0.03 * (medium_hits - 1)

    sarcasm = float(mods.get("sarcasm_mod", 0.5) or 0.5)
    enthusiasm = float(mods.get("enthusiasm_mod", 0.6) or 0.6)
    fatigue = float(mods.get("fatigue_mod", 0.0) or 0.0)
    stress = float(mods.get("stress_mod", 0.0) or 0.0)

    score += 0.08 * (sarcasm - 0.5)
    score += 0.05 * (enthusiasm - 0.5)
    score += 0.06 * stress
    score -= 0.10 * fatigue

    score = max(0.0, min(score, 1.0))

    if score >= 0.85:
        decision = True
    elif score <= 0.15:
        decision = False
    else:
        roll = random.random()
        logger.debug(
            "tg_post_manager pacing decision bucket=%s mood=%s intensity=%s score=%.2f roll=%.2f",
            tb,
            mood,
            intensity,
            score,
            roll,
        )
        decision = roll <= score

    logger.info(
        "tg_post_manager pacing summary bucket=%s mood=%s intensity=%s score=%.2f -> %s (keywords=%r)",
        tb,
        mood,
        intensity,
        score,
        "POST" if decision else "SKIP",
        keywords,
    )
    return decision


def _should_add_dm_cta_hint(time_bucket: str, mood: str, intensity: str) -> bool:

    tb = (time_bucket or "").lower()
    mood = (mood or "").lower()
    intensity = (intensity or "").lower()

    base = 0.06

    if tb == "morning":
        base += 0.02
    elif tb == "day":
        base += 0.05
    elif tb == "evening":
        base += 0.18

    if mood in {"scandal", "escalation", "tense"}:
        base += 0.07
    if intensity == "high":
        base += 0.07
    elif intensity == "low":
        base -= 0.03

    base = max(0.0, min(base, 0.45))

    roll = random.random()
    logger.debug(
        "tg_post_manager DM CTA decision bucket=%s mood=%s intensity=%s prob=%.2f roll=%.2f",
        tb,
        mood,
        intensity,
        base,
        roll,
    )
    return roll <= base


async def _fetch_news_digest() -> str:

    news_prompt = (
        "Provide 10 concise bullet points on today's most discussed political and economic news. "
        "Include: (a) events involving the US, EU, UK, NATO or other Western countries; "
        "(b) significant developments involving Russia (domestic politics, economy, diplomacy, or conflicts); "
        "(c) if relevant, 1-2 global events that affect both Russia and Western countries. "
        "Whenever possible, let several bullets relate to the same 2–3 key themes of the day "
        "(for example sanctions, elections, protests, front line, major economic shocks) "
        "instead of 10 completely unrelated stories. "
        "For each bullet, start with one of the labels [WEST], [RU], or [GLOBAL]. "
        "Focus strictly on factual descriptions of events and decisions based on reputable news outlets, "
        "not on rumours, anonymous Telegram posts or social-media threads. "
        "Each bullet up to 2 sentences. "
        "Exclude any recommendations about how people should vote, protest, donate or engage politically. "
        "Return only the bullet list."
    )

    system_text = (
        "You are a neutral international news editor. "
        "You summarize world news in a strictly factual tone. "
        "When using web_search, rely primarily on large, reputable international and Russian news outlets "
        "and agencies (for example: Reuters, Associated Press, AFP, BBC, DW, Financial Times, "
        "The New York Times, The Wall Street Journal, Al Jazeera, Le Monde; for Russian-related topics: "
        "Интерфакс, РБК, «Коммерсантъ»). "
        "Prefer sources that do original reporting and official statements. "
        "Avoid anonymous blogs, social-media threads, opinionated Telegram channels or obviously low-credibility sites. "
        "If information is unclear or conflicting, say so instead of speculating."
    )

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.POST_MODEL,
                input=_to_responses_input(
                    [
                        {"role": "system", "content": system_text},
                        {"role": "user", "content": news_prompt},
                    ]
                ),
                tools=[{"type": "web_search"}],
                tool_choice={"type": "auto"},
                max_output_tokens=700,
                temperature=0.5,
            ),
            timeout=settings.POST_MODEL_TIMEOUT,
        )
        digest = (_get_output_text(resp) or "").strip()
        return digest
    except asyncio.TimeoutError:
        logger.warning("tg_post_manager news digest timed out")
    except Exception:
        logger.exception("tg_post_manager failed to fetch news digest")

    return ""


async def _analyze_news_context(news_snippet: str) -> dict:

    default = {
        "focus": "MIXED",
        "mood": "routine",
        "intensity": "medium",
        "keywords": "",
    }
    if not news_snippet or not news_snippet.strip():
        return default

    model = getattr(settings, "RESPONSE_MODEL", getattr(settings, "POST_MODEL", None))
    if not model:
        return default

    prompt = (
        "You are an analytical assistant. You receive a bullet list of today's news, where each bullet starts "
        "with [WEST], [RU], or [GLOBAL]. Your task is to characterize the overall situation.\n\n"
        "Decide:\n"
        "1) FOCUS: which block dominates the day – WEST, RU, GLOBAL, or MIXED if there is no clear dominance.\n"
        "2) MOOD: one word from {routine, calm, scandal, escalation, tense, economic_crisis} that best fits the "
        "overall tone of events.\n"
        "3) INTENSITY: low, medium, or high – how sharp and conflict-driven the news cycle is.\n"
        "4) KEYWORDS: 3–6 short Russian descriptors (comma-separated) summarizing the main themes.\n\n"
        "Return EXACTLY four lines in this format:\n"
        "FOCUS: <WEST|RU|GLOBAL|MIXED>\n"
        "MOOD: <routine|calm|scandal|escalation|tense|economic_crisis>\n"
        "INTENSITY: <low|medium|high>\n"
        "KEYWORDS: <comma-separated short Russian phrases>"
    )

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=model,
                input=_to_responses_input(
                    [
                        {
                            "role": "system",
                            "content": "You are a precise, concise classifier.",
                        },
                        {
                            "role": "user",
                            "content": f"{prompt}\n\nNEWS BULLETS:\n{news_snippet}",
                        },
                    ]
                ),
                max_output_tokens=160,
                temperature=0.2,
            ),
            timeout=min(getattr(settings, "RESPONSE_MODEL_TIMEOUT", 15), 30),
        )
        text = (_get_output_text(resp) or "").strip()
    except Exception:
        logger.exception("tg_post_manager _analyze_news_context failed")
        return default

    focus = default["focus"]
    mood = default["mood"]
    intensity = default["intensity"]
    keywords = default["keywords"]

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("FOCUS:"):
            val = upper.split(":", 1)[1].strip()
            if val in {"WEST", "RU", "GLOBAL", "MIXED"}:
                focus = val
        elif upper.startswith("MOOD:"):
            val = line.split(":", 1)[1].strip().lower()
            if val in {"routine", "calm", "scandal", "escalation", "tense", "economic_crisis"}:
                mood = val
        elif upper.startswith("INTENSITY:"):
            val = line.split(":", 1)[1].strip().lower()
            if val in {"low", "medium", "high"}:
                intensity = val
        elif upper.startswith("KEYWORDS:"):
            val = line.split(":", 1)[1].strip()
            keywords = val

    result = {
        "focus": focus,
        "mood": mood,
        "intensity": intensity,
        "keywords": keywords,
    }
    logger.info("tg_post_manager news context analysis %s", result)
    return result


def _weighted_choice(weight_map: dict[str, float]) -> str:

    items: list[tuple[str, float]] = []
    for k, v in (weight_map or {}).items():
        try:
            w = float(v)
        except Exception:
            w = 0.0
        if w < 0:
            w = 0.0
        items.append((k, w))

    if not items:
        return random.choice(POST_TYPES)

    total = sum(w for _, w in items)
    if total <= 0:
        return random.choice([name for name, _ in items])

    r = random.uniform(0, total)
    acc = 0.0
    for name, w in items:
        acc += w
        if r <= acc:
            return name

    return items[-1][0]


def _pick_post_type(mood: str, focus: str, mods: dict, time_bucket: str) -> str:

    mood = (mood or "").lower()
    focus = (focus or "").upper()
    time_bucket = (time_bucket or "").lower()

    weights: dict[str, float] = {name: 1.0 for name in POST_TYPES}

    sarcasm = float(mods.get("sarcasm_mod", 0.5) or 0.5)
    enthusiasm = float(mods.get("enthusiasm_mod", 0.6) or 0.6)
    fatigue = float(mods.get("fatigue_mod", 0.0) or 0.0)
    creativity = float(mods.get("creativity_mod", 0.6) or 0.6)
    precision = float(mods.get("precision_mod", 0.5) or 0.5)

    if mood in {"scandal", "escalation", "tense"}:
        for name in [
            "краткий эмоциональный спич",
            "диалог с телевизором",
            "саркастический итог дня",
            "короткая язвительная реакция",
        ]:
            weights[name] = weights.get(name, 1.0) + 1.2
    elif mood in {"economic_crisis"}:
        for name in [
            "краткий эмоциональный спич",
            "ироничный комментарий с намёком на историю",
            "спокойное, но жёсткое замечание",
        ]:
            weights[name] = weights.get(name, 1.0) + 0.9
    elif mood in {"calm", "routine"}:
        for name in [
            "спокойное, но жёсткое замечание",
            "ироничный комментарий с намёком на историю",
            "сравнение с прошлыми событиями",
        ]:
            weights[name] = weights.get(name, 1.0) + 0.8

    if focus == "RU":
        for name in [
            "сравнение с прошлыми событиями",
            "спокойное, но жёсткое замечание",
        ]:
            weights[name] = weights.get(name, 1.0) + 0.7
    elif focus == "WEST":
        for name in [
            "короткая язвительная реакция",
            "диалог с телевизором",
        ]:
            weights[name] = weights.get(name, 1.0) + 0.9
    elif focus == "GLOBAL":
        name = "краткий эмоциональный спич"
        weights[name] = weights.get(name, 1.0) + 0.6

    weights["саркастический итог дня"] *= 0.8 + 1.6 * sarcasm
    weights["короткая язвительная реакция"] *= 0.9 + 1.3 * sarcasm

    weights["краткий эмоциональный спич"] *= 0.8 + 1.5 * enthusiasm
    weights["диалог с телевизором"] *= 0.8 + 1.4 * ((sarcasm + enthusiasm) / 2)

    calm_boost = 0.8 + 1.2 * fatigue
    for name in [
        "спокойное, но жёсткое замечание",
        "ироничный комментарий с намёком на историю",
    ]:
        weights[name] = weights.get(name, 1.0) * calm_boost

    weights["сравнение с прошлыми событиями"] *= 0.9 + 1.2 * (
        (creativity + precision) / 2
    )

    if time_bucket == "morning":
        for name in [
            "короткая язвительная реакция",
            "ироничный комментарий с намёком на историю",
        ]:
            weights[name] = weights.get(name, 1.0) * 1.2
    elif time_bucket == "day":
        for name in [
            "спокойное, но жёсткое замечание",
            "сравнение с прошлыми событиями",
        ]:
            weights[name] = weights.get(name, 1.0) * 1.15
    elif time_bucket == "evening":
        for name in [
            "диалог с телевизором",
            "саркастический итог дня",
        ]:
            weights[name] = weights.get(name, 1.0) * 1.25

    return _weighted_choice(weights)


def _pick_structure_mode(mood: str, intensity: str, time_bucket: str) -> str:
    
    mood = (mood or "").lower()
    intensity = (intensity or "").lower()
    tb = (time_bucket or "").lower()

    weights: dict[str, float] = {
        "default": 0.6,
        "lightning": 0.15,
        "extended_monologue": 0.15,
        "ladder": 0.10,
    }

    if intensity == "high":
        weights["lightning"] += 0.07
        weights["ladder"] += 0.03
    elif intensity == "low":
        weights["extended_monologue"] -= 0.03

    if mood in {"scandal", "escalation"}:
        weights["lightning"] += 0.05
        weights["ladder"] += 0.05
    elif mood in {"economic_crisis"}:
        weights["ladder"] += 0.05
        weights["extended_monologue"] += 0.03
    elif mood in {"calm", "routine"}:
        weights["extended_monologue"] += 0.04

    if tb == "morning":
        weights["lightning"] += 0.05
    elif tb == "evening":
        weights["extended_monologue"] += 0.05
        weights["ladder"] += 0.03

    for k, v in list(weights.items()):
        if v < 0:
            weights[k] = 0.0

    return _weighted_choice(weights)


async def _send_telegram_with_retry(chat_id: int, text: str) -> None:
    bot = get_bot()
    attempt = 1
    while True:
        try:
            await bot.send_message(
                chat_id,
                text,
                disable_web_page_preview=True,
            )
            return
        except TelegramRetryAfter as e:
            delay = max(1, int(getattr(e, "retry_after", 5)))
            logger.warning(
                "tg_post_manager TelegramRetryAfter, sleep %ss (attempt %d)",
                delay,
                attempt,
            )
            await asyncio.sleep(delay)
            attempt += 1
        except TelegramBadRequest as e:
            logger.warning("tg_post_manager TelegramBadRequest: %s", e)
            return
        except Exception as e:
            if attempt >= 3:
                logger.exception(
                    "tg_post_manager send_message failed after %d attempts: %s",
                    attempt,
                    e,
                )
                return
            await asyncio.sleep(1.5 * attempt)
            attempt += 1


async def generate_and_post_tg() -> None:

    persona_chat_id = getattr(settings, "TG_PERSONA_CHAT_ID", None)
    if not persona_chat_id:
        logger.warning("tg_post_manager persona_chat_id is not configured; skip")
        return

    raw_channel_id = settings.TG_CHANNEL_ID
    if not raw_channel_id:
        logger.warning(
            "tg_post_manager TG_CHANNEL_ID is not configured; skip"
        )
        return

    try:
        channel_id = int(raw_channel_id)
    except (TypeError, ValueError):
        logger.warning(
            "tg_post_manager invalid channel id value %r; skip", raw_channel_id
        )
        return

    time_bucket, rubric_label = _get_time_bucket()
    start_hour = getattr(settings, "SCHED_TG_START_HOUR", 8)
    end_hour = getattr(settings, "SCHED_TG_END_HOUR", 23)

    if time_bucket == "night":
        logger.info(
            "tg_post_manager skip posting – outside corridor %02d:00–%02d:00 (bucket=%s)",
            start_hour,
            end_hour,
            time_bucket,
        )
        return

    persona = await get_persona(persona_chat_id)
    try:
        await asyncio.wait_for(persona._restored_evt.wait(), timeout=5.0)
    except Exception:
        logger.exception("tg_post_manager persona restore failed")

    try:
        history = await load_context(persona_chat_id, persona_chat_id)
    except Exception:
        logger.exception("tg_post_manager load_context failed")
        history = []
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]

    recent_posts = _extract_recent_posts(history, limit=RECENT_POSTS_FOR_CONTEXT)
    recent_texts_full = _extract_recent_assistant_texts(history, limit=RECENT_POSTS_FOR_CONTEXT)

    try:
        style_mods = persona._mods_cache or await asyncio.wait_for(
            persona.style_modifiers(), 30
        )
    except Exception:
        logger.exception("tg_post_manager style_modifiers acquisition failed")
        style_mods = {}
    mods = _merge_and_clamp_mods(style_mods)

    try:
        guidelines = await persona.style_guidelines(persona_chat_id)
    except Exception:
        logger.exception("tg_post_manager style_guidelines acquisition failed")
        guidelines = ""

    novelty = (
        0.4 * mods["creativity_mod"]
        + 0.4 * mods["sarcasm_mod"]
        + 0.2 * mods["enthusiasm_mod"]
    )
    coherence = (
        0.5 * mods["confidence_mod"]
        + 0.3 * mods["precision_mod"]
        + 0.1 * (1 - mods["fatigue_mod"])
        + 0.1 * (1 - mods["stress_mod"])
    )
    alpha = 1.8
    dynamic_temperature = MIN_TEMPERATURE + (MAX_TEMPERATURE - MIN_TEMPERATURE) * (
        novelty**alpha
    )
    dynamic_top_p = TOP_P_MIN + (TOP_P_MAX - TOP_P_MIN) * (1.0 - coherence)

    try:
        dynamic_temperature *= 1.0 + 0.10 * float(mods["valence_mod"])
    except Exception:
        pass

    if time_bucket == "morning":
        dynamic_temperature = max(0.57, min(dynamic_temperature + 0.02, 0.74))

    if dynamic_temperature < 0.55:
        dynamic_temperature = 0.55
    if dynamic_temperature > 0.74:
        dynamic_temperature = 0.74
    if dynamic_top_p < 0.85:
        dynamic_top_p = 0.85
    if dynamic_top_p > 0.98:
        dynamic_top_p = 0.98

    news_snippet = await _fetch_news_digest()
    news_context = await _analyze_news_context(news_snippet)

    focus = news_context.get("focus", "MIXED")
    mood = news_context.get("mood", "routine")
    intensity = news_context.get("intensity", "medium")
    keywords = news_context.get("keywords", "")

    if not news_snippet.strip():
        mood = "calm"
        intensity = "low"
        keywords = keywords or "тихий новостной день, фоновое ворчание"

    if not _should_post_now(time_bucket, mood, intensity, keywords, mods):
        logger.info(
            "tg_post_manager skip posting by contextual pacing "
            "(bucket=%s focus=%s mood=%s intensity=%s keywords=%r)",
            time_bucket,
            focus,
            mood,
            intensity,
            keywords,
        )
        return

    if intensity == "high":
        dynamic_temperature = min(dynamic_temperature + 0.03, MAX_TEMPERATURE)
    elif intensity == "low":
        dynamic_temperature = max(dynamic_temperature - 0.03, MIN_TEMPERATURE)

    if dynamic_temperature < 0.55:
        dynamic_temperature = 0.55
    if dynamic_temperature > 0.80:
        dynamic_temperature = 0.80

    dm_cta_enabled = _should_add_dm_cta_hint(time_bucket, mood, intensity)

    try:
        last_cta_at = _last_dm_cta_time(history)
    except Exception:
        last_cta_at = None

    if last_cta_at is not None:
        if (_get_local_now() - last_cta_at) < timedelta(hours=6):
            dm_cta_enabled = False

    try:
        logger.info(
            "ZHIRIK mods and sampling: time_bucket=%s focus=%s mood=%s intensity=%s "
            "novelty=%.3f coherence=%.3f temp=%.2f top_p=%.2f "
            "mods[c/sa/e/conf/prec/fat/str/val]=[%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f]",
            time_bucket,
            focus,
            mood,
            intensity,
            novelty,
            coherence,
            dynamic_temperature,
            dynamic_top_p,
            mods["creativity_mod"],
            mods["sarcasm_mod"],
            mods["enthusiasm_mod"],
            mods["confidence_mod"],
            mods["precision_mod"],
            mods["fatigue_mod"],
            mods["stress_mod"],
            mods["valence_mod"],
        )
    except Exception:
        pass

    try:
        system_base = await build_system_prompt(persona, guidelines, user_gender=None)
    except Exception:
        logger.exception("tg_post_manager build_system_prompt failed")
        system_base = "You are a helpful assistant."

    system_msg = {
        "role": "system",
        "content": (
            system_base
            + "\nПредставь, что ты ведёшь авторское коммент-шоу «Диалог с телевизором»: "
              "говоришь от первого лица, эмоционально, саркастично, короткими ударными фразами, "
              "как человек, который спорит с новостями вслух.\n"
            "Сейчас твоя задача — подготовить короткий пост для публичного телеграм-канала на русском языке, "
              "где ты кратко комментируешь новости о Западе и о России.\n"
            "Ограничения для постов:\n"
            "- не агитируй и не призывай голосовать, протестовать, донатить или вступать в какие-либо политические организации;\n"
            "- не призывай к насилию, войне, репрессиям или дискриминации;\n"
            "- не призывай поддерживать или не поддерживать конкретные партии, политиков, движения;\n"
            "- не призывай к свержению власти, революции, переворотам;\n"
            "- можешь жёстко критиковать идеи, решения, правительства и институты, "
              "но не оскорбляй людей по национальности, расе, религии, полу и другим личным признакам;\n"
            "- делись своим взглядом на происходящее, а не давай аудитории инструкции, что делать или как голосовать;\n"
            "- все тексты только на русском языке;\n"
            "- не выдумывай события, законы, выборы или факты; если точной информации нет, говори об этом прямо "
              "или формулируй абстрактно («в такие моменты обычно происходит…»), без придумывания конкретики;\n"
            "- не используй хэштеги и эмодзи.\n"
            "Иногда мягко напоминай, что ты политик, которого уже воспринимают как часть истории и как мем: "
            "допускается лёгкая самоирония над собственным образом, без перечисления конкретных реальных эпизодов, "
            "лозунгов и точных цитат."
        ),
    }

    if recent_posts:
        history_block = (
            "Краткая хронология твоих последних постов (от старых к новым):\n"
            f"{recent_posts}\n\n"
            "Используй эту хронологию для ощущения непрерывного голоса: если новая новость логически связана "
            "с тем, что ты уже говорил, можешь коротко сослаться на прошлое "
            "(например: «я же тогда говорил…», «ещё весной предупреждал…»). "
            "Не копируй буквально формулировки прошлых постов: сохраняй смысл, но меняй образы, конструкции и формулировки.\n"
            "Если несколько последних новостей крутятся вокруг одной темы (санкции, фронт, выборы, протесты, экономика), "
            "относись к этим постам как к мини-сериалу: иногда продолжай линию и усиливай её, а не начинай с нуля.\n\n"
        )
    else:
        history_block = (
            "Раньше ты уже много раз комментировал новости, но сейчас можешь начать новый виток "
            "с кратким напоминанием, какую хронику ты ведёшь. "
            "Избегай однообразных формулировок, экспериментируй с подачей.\n\n"
        )

    focus_labels_ru = {
        "WEST": "западная повестка",
        "RU": "события внутри России",
        "GLOBAL": "глобальные процессы",
        "MIXED": "пересечение российской и западной повестки",
    }
    mood_labels_ru = {
        "routine": "обычный бюрократический день",
        "calm": "относительно спокойный день",
        "scandal": "скандальный, нервный день",
        "escalation": "день обострения и жёстких решений",
        "tense": "напряжённый день с нервозной риторикой",
        "economic_crisis": "день экономических тревог и провалов",
    }

    focus_ru = focus_labels_ru.get(focus, "политическая повестка дня")
    mood_ru = mood_labels_ru.get(mood, "обычный политический день")
    keywords_ru = keywords or "общая политическая повестка"

    if dm_cta_enabled:
        cta_hint = (
            "- в последней фразе можешь мягко намекнуть, что спорить и разбирать детали с тобой "
            "проще в личке, чем с телевизором; делай это одной короткой разговорной фразой, "
            "без упоминания кнопок, ссылок и команд и без прямого перечисления действий, "
            "которые надо совершить;\n"
        )
    else:
        cta_hint = ""

    try:
        self_meme_enabled = random.random() < 0.12
    except Exception:
        self_meme_enabled = False

    if self_meme_enabled:
        self_meme_hint = (
            "- в конце этого поста добавь одну короткую самоироничную ремарку про себя как про "
            "«политика из прошлого, который всё ещё комментирует настоящее», без упоминания конкретных лозунгов, "
            "реальных эпизодов и точных цитат;\n"
        )
    else:
        self_meme_hint = ""

    context_block = (
        "Анализ новостного дня:\n"
        f"- основной фокус: {focus_ru};\n"
        f"- обстановка: {mood_ru}, интенсивность: {intensity};\n"
        f"- ключевые темы: {keywords_ru}.\n\n"
        "Эмоциональность и направление поста должны соответствовать этому контексту, "
        "а не случайному настроению.\n\n"
    )

    news_block = (
        "У тебя есть список свежих международных новостей про США, Европу, НАТО и другие западные страны, "
        "а также важные события, связанные с Россией.\n"
        "Вот краткое резюме (маркированный список):\n"
        f"{news_snippet or '— сегодня лента тихая и скучная, почти ничего яркого.'}\n\n"
    )

    mood_style_hints = {
        "scandal": "День скандальный и нервный: допускается больше колкой иронии и удивления, но без истерики.",
        "escalation": "День обострения: твой тон может быть жёстким, но сдержанным, без призывов к действиям.",
        "tense": "День напряжённый: можно добавить ощущение нервозности в формулировках.",
        "economic_crisis": "День экономической тревоги: фокус на цифрах, провалах, последствиях для людей.",
        "calm": "День относительно спокойный: можно позволить себе более ироничный, размеренный тон.",
        "routine": "Обычный бюрократический день: больше усталого сарказма к рутине и формальности.",
    }

    focus_style_hints = {
        "WEST": "Фокус на западной повестке: покажи, что ты смотришь на неё со своей российской оптикой.",
        "RU": "Фокус на внутренних событиях России: говоришь как человек, который живёт внутри этой страны.",
        "GLOBAL": "Фокус на глобальных трендах: покажи, как это выглядит с российской колокольни.",
        "MIXED": "Фокус на пересечении российской и западной повестки: подчеркни двойные стандарты и повторяемость сюжетов.",
    }

    mood_hint = mood_style_hints.get(mood, "")
    focus_hint = focus_style_hints.get(focus, "")

    post_type = _pick_post_type(mood, focus, mods, time_bucket)
    post_style_hint = POST_TYPE_INSTRUCTIONS.get(post_type, "")

    structure_mode = _pick_structure_mode(mood, intensity, time_bucket)

    effective_char_limit = POST_CHAR_LIMIT
    if post_type == "диалог с телевизором":
        effective_char_limit = int(POST_CHAR_LIMIT * 1.1)
        effective_char_limit = min(effective_char_limit, POST_CHAR_LIMIT + 80)

    if structure_mode == "lightning":
        effective_char_limit = min(effective_char_limit, 260)
    elif structure_mode == "ladder":
        effective_char_limit = min(effective_char_limit, 480)

    final_char_limit = effective_char_limit
    
    hook_type = random.choice(HOOK_TYPES)
    hook_hint = HOOK_TYPE_HINTS.get(hook_type, "")
    hook_hint_line = (
        f"- ориентир для такого крючка: {hook_hint}\n" if hook_hint else ""
    )

    dialog_format_hint = ""
    if post_type == "диалог с телевизором":
        dialog_format_hint = (
            "Формат для этого типа поста:\n"
            "- оформи пост как мини-диалог 2–4 реплики;\n"
            "- используй ровно две роли: «ТЕЛЕВИЗОР:» и «Я:»;\n"
            "- каждую реплику выводи с новой строки, без дополнительных пояснений и ремарок.\n"
            "Пример структуры (это только схема, не текст для копирования):\n"
            "ТЕЛЕВИЗОР: ...\n"
            "Я: ...\n\n"
        )

    if structure_mode == "lightning":
        hook_lines = (
            "- даже если весь пост состоит из одной строки, она должна быть крючком: парадокс, образ, короткий вопрос к себе или неожиданный оборот, который хочется переслать;\n"
            "- если строк больше одной, первую делай отдельной короткой фразой (примерно до 100 символов) и ставь перенос строки после неё, чтобы она работала как заголовок;\n"
        )
        length_hint_line = (
            "- в этом посте можешь ограничиться одной-двумя короткими фразами; если делаешь одну фразу, пусть она читается как ударная молния;\n"
        )
        structure_hint_line = (
            "- не растягивай мысль на абзацы: максимум плотности в минимуме текста.\n"
        )
    elif structure_mode == "extended_monologue":
        hook_lines = (
            "- первые 1-2 строки должны быть крючком: парадокс, образ, короткий вопрос к себе или неожиданный оборот, который хочется дочитать;\n"
            "- первую строку делай отдельной короткой фразой (примерно до 100 символов) и ставь перенос строки после неё, чтобы она работала как заголовок;\n"
        )
        length_hint_line = (
            "- в этот раз можешь сделать более развёрнутый мини-монолог: примерно 4–6 коротких, энергичных предложений;\n"
        )
        structure_hint_line = (
            "- выстрой мысль как цепочку: от повода к паре промежуточных шагов и к короткому выводу.\n"
        )
    elif structure_mode == "ladder":
        hook_lines = (
            "- первые 1-2 строки должны быть крючком: парадокс, образ, короткий вопрос к себе или неожиданный оборот, который хочется дочитать;\n"
            "- первую строку делай отдельной короткой фразой (примерно до 100 символов) и ставь перенос строки после неё, чтобы она работала как заголовок;\n"
        )
        length_hint_line = (
            "- оформи текст как 2–3 короткие строки-«ступеньки», каждая с новой строки;\n"
        )
        structure_hint_line = (
            "- часто начинай строки словами «Сначала…», «Потом…», «В итоге…» или похожими по смыслу, но без маркеров списка и форматирования.\n"
        )
    else:
        hook_lines = (
            "- первые 1-2 строки должны быть крючком: парадокс, образ, короткий вопрос к себе или неожиданный оборот, который хочется дочитать;\n"
            "- первую строку делай отдельной короткой фразой (примерно до 100 символов) и ставь перенос строки после неё, чтобы она работала как заголовок;\n"
        )
        length_hint_line = (
            "- обычно 2–4 предложения; избегай длинных, тяжёлых конструкций;\n"
        )
        structure_hint_line = ""

    user_prompt = (
        history_block
        + news_block
        + context_block
        + "Задача: придумать ОДИН пост для телеграм-канала от своего собственного лица (Жириновский В.В.).\n"
        f"Рубрика по времени суток: {rubric_label}.\n"
        f"Тип поста: {post_type}.\n"
        f"{post_style_hint}\n\n"
        f"{dialog_format_hint}"
        f"{mood_hint}\n"
        f"{focus_hint}\n\n"
        "Требования к посту:\n"
        "- выбери одну самую показательную новость из списка или общий мотив дня (это может быть как западная повестка, так и российская), "
        "так чтобы это соответствовало описанному выше фокусу и настроению;\n"
        f"{hook_lines}"
        f"- для крючка выбери формат: {hook_type};\n"
        f"{hook_hint_line}"
        f"- до {effective_char_limit} символов;\n"
        "- длина поста может колебаться: иногда делай его короче, иногда ближе к лимиту, по внутренней логике ситуации;\n"
        f"{length_hint_line}"
        "- можно сделать структуру «завязка — взрыв — короткий вывод» или мини-диалог с телевизором;\n"
        f"{structure_hint_line}"
        "- тон и энергия текста должны соответствовать рубрике по времени суток: утром чуть бодрее и короче, вечером можно чуть подробнее и с ощущением подведения итогов;\n"
        "- разговорный язык, допускается сарказм, преувеличения и жёсткие формулировки, но без мата;\n"
        "- не давай призывов к политическим действиям (голосовать, выходить на митинги, присоединяться к акциям, вступать в организации, донатить и т.п.), "
        "просто выскажи своё мнение и настроение момента;\n"
        f"{cta_hint}"
        f"{self_meme_hint}"
        "- можешь коротко напомнить, что похожее уже происходило раньше, если это логично, "
        "особенно когда речь идёт о повторяющихся решениях Запада или о старых российских проблемах;\n"
        "- не повторяй почти дословно формулировки твоих прошлых постов из хронологии выше, меняй подачу и образы;\n"
        "- никакого форматирования, без кавычек вокруг текста, без пояснений от автора.\n"
        "Ответь только текстом поста, в формате личного высказывания."
    )

    safe_history: list[dict] = []
    for m in history:
        if m.get("role") == "system":
            content = m.get("content") or ""
            if isinstance(content, str) and content.startswith(CTA_MARKER_PREFIX):
                continue
        safe_history.append(m)

    messages = [system_msg] + safe_history + [{"role": "user", "content": user_prompt}]

    post_text = None
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.POST_MODEL,
                input=_to_responses_input(messages),
                temperature=dynamic_temperature,
                top_p=dynamic_top_p,
                max_output_tokens=600,
            ),
            timeout=settings.POST_MODEL_TIMEOUT,
        )
        post_text = (_get_output_text(resp) or "").strip()
    except asyncio.TimeoutError:
        logger.warning("tg_post_manager post generation timed out")
    except Exception:
        logger.exception("tg_post_manager failed to generate post")

    if not post_text:
        post_text = (
            "Запад снова шумит, Россия снова держит удар, а хроника одна и та же: "
            "одни делают вид, что ничего не помнят, другие помнят слишком хорошо."
        )
    else:
        if _too_similar_to_recent(post_text, recent_texts_full) or _first_line_too_similar(post_text, recent_texts_full):
            logger.info(
                "tg_post_manager post too similar to recent history, try regenerate with slightly higher temperature"
            )
            retry_temperature = min(dynamic_temperature + 0.05, MAX_TEMPERATURE)
            retry_top_p = min(dynamic_top_p + 0.02, TOP_P_MAX)
            try:
                resp2 = await asyncio.wait_for(
                    _call_openai_with_retry(
                        endpoint="responses.create",
                        model=settings.POST_MODEL,
                        input=_to_responses_input(messages),
                        temperature=retry_temperature,
                        top_p=retry_top_p,
                        max_output_tokens=600,
                    ),
                    timeout=settings.POST_MODEL_TIMEOUT,
                )
                new_text = (_get_output_text(resp2) or "").strip()
                if new_text:
                    post_text = new_text
            except asyncio.TimeoutError:
                logger.warning("tg_post_manager post regeneration timed out")
            except Exception:
                logger.exception("tg_post_manager failed to regenerate post")

    if len(post_text) > final_char_limit:
        cut = final_char_limit - len(TRIM_SUFFIX)
        if cut <= 0:
            post_text = TRIM_SUFFIX
        else:
            post_text = post_text[:cut].rstrip() + TRIM_SUFFIX

    if _contains_direct_call_to_action(post_text):
        logger.warning(
            "tg_post_manager: generated post содержит прямой призыв к действию, пробуем переписать без призывов"
        )
        rewritten = await _rewrite_post_without_calls(post_text, final_char_limit)
        if rewritten and not _contains_direct_call_to_action(rewritten):
            logger.info("tg_post_manager: post успешно переписан без призывов")
            post_text = rewritten
        else:
            logger.warning(
                "tg_post_manager: не удалось безопасно переписать пост без призывов, пост не будет отправлен"
            )
            return

    logger.info(
        "tg_post_manager final post length=%d chars, type=%s focus=%s mood=%s intensity=%s structure=%s text=%r",
        len(post_text),
        post_type,
        focus,
        mood,
        intensity,
        structure_mode,
        post_text,
    )

    try:
        await _send_telegram_with_retry(channel_id, post_text)
    except Exception:
        logger.exception("tg_post_manager failed to send Telegram message")
        return

    try:
        extra_tasks = [
            persona.process_interaction(persona_chat_id, post_text),
            push_message(
                persona_chat_id,
                "assistant",
                post_text,
                user_id=persona_chat_id,
            ),
        ]
        if dm_cta_enabled:
            extra_tasks.append(
                push_message(
                    persona_chat_id,
                    "system",
                    f"{CTA_MARKER_PREFIX}{_get_local_now().isoformat()}",
                    user_id=persona_chat_id,
                )
            )
        await asyncio.gather(*extra_tasks)
    except Exception:
        logger.exception("tg_post_manager saving to memory failed")