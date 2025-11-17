#app/services/addons/voice_generator.py
from __future__ import annotations

import asyncio
import logging
import os
import random
import zlib
import re
import atexit
import tempfile
import contextlib
import unicodedata
import asyncio.subprocess as asp

import time as time_module
from typing import Optional, Dict, Any, Tuple

from aiogram.enums import ChatAction
from aiogram.exceptions import (
    TelegramBadRequest, TelegramRetryAfter, TelegramForbiddenError,
    TelegramNetworkError
)
from aiogram.types import FSInputFile

from app.config import settings
from app.clients.telegram_client import get_bot
from app.clients.elevenlabs_client import ElevenLabsClient
from app.bot.components.constants import redis_client
from app.emo_engine import get_persona

logger = logging.getLogger(__name__)

_ELEVENLABS_API_KEY_PRESENT = bool(getattr(settings, "ELEVENLABS_API_KEY", ""))
TTS_ENABLED = bool(getattr(settings, "TTS_ENABLED", True)) and _ELEVENLABS_API_KEY_PRESENT
TTS_PROB_TEXT = float(getattr(settings, "TTS_PROBABILITY_TEXT", 0.07))
TTS_PROB_VOICEIN = float(getattr(settings, "TTS_PROBABILITY_VOICEIN", 0.66))
TTS_USE_EMOTIONS = bool(getattr(settings, "TTS_USE_EMOTIONS", False))
TTS_EMO_BLEND    = float(getattr(settings, "TTS_EMO_BLEND", 0.0))

MAX_TTS_CHARS = int(getattr(settings, "ELEVENLABS_MAX_TTS_CHARS", 900))
MIN_TTS_CHARS = int(getattr(settings, "MIN_TTS_CHARS", 8))

SSML_ENABLE  = bool(getattr(settings, "ELEVENLABS_ENABLE_SSML_BREAKS", True))
SSML_MODE    = str(getattr(settings, "ELEVENLABS_SSML_MODE", "on")).lower()   # auto|on|off
PHONEMES_EN  = bool(getattr(settings, "ELEVENLABS_ENABLE_PHONEMES_EN", False))

MIN_TTS_CHARS_VOICEIN = int(getattr(settings, "MIN_TTS_CHARS_VOICEIN", 2))
MAX_TTS_SENTENCES = int(getattr(settings, "MAX_TTS_SENTENCES", 5))
MAX_TTS_CHARS_STRICT = int(getattr(settings, "MAX_TTS_CHARS_STRICT", 100))
LANG_DETECT_HINT_MIN = int(getattr(settings, "LANG_DETECT_HINT_MIN", 8))

LANG_UI_REDIS_KEY_FMT  = "lang:{user_id}"
LANG_TTS_REDIS_KEY_FMT = "lang_tts:{user_id}"

_EMOJI_RX = re.compile(r"[\U0001F300-\U0001FAFF]")
_CYR_RX   = re.compile(r"[А-Яа-яЁёІіЇїЄєґҐ]")
_HEB_RX   = re.compile(r"[\u0590-\u05FF]")
_AR_RX    = re.compile(r"[\u0600-\u06FF]")
_JA_RX    = re.compile(r"[\u3040-\u30FF]")  # Hiragana + Katakana
_KO_RX    = re.compile(r"[\u1100-\u11FF\uAC00-\uD7AF]")  # Jamo + Hangul syllables
_CJK_RX   = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")  # Han ideographs (mostly zh)
_JA_PUNCT_RX = re.compile(r"[、。〜・「」『』【】]")
_DEV_RX  = re.compile(r"[\u0900-\u097F]")  # Devanagari (hi)
_EL_RX   = re.compile(r"[Α-Ωα-ω]")        # Greek (el)
_TH_RX   = re.compile(r"[\u0E00-\u0E7F]") # Thai (th)

# Паузы по смысловым маркерам
_CLAUSE_BREAK_RU = re.compile(r"\b(однако|но|зато|впрочем|если|когда|потому что|кстати|между прочим)\b", re.IGNORECASE)
_CLAUSE_BREAK_EN = re.compile(r"\b(however|but|though|anyway|because|when|by the way)\b", re.IGNORECASE)
_CLAUSE_BREAK_ES = re.compile(r"\b(sin embargo|pero|aunque|porque|cuando|por cierto)\b", re.IGNORECASE)
_CLAUSE_BREAK_PT = re.compile(r"\b(porém|mas|embora|porque|quando|aliás)\b", re.IGNORECASE)
_CLAUSE_BREAK_FR = re.compile(r"\b(cependant|mais|pourtant|parce que|quand|d'ailleurs)\b", re.IGNORECASE)
_CLAUSE_BREAK_DE = re.compile(r"\b(jedoch|aber|obwohl|weil|wenn|übrigens)\b", re.IGNORECASE)
_CLAUSE_BREAK_IT = re.compile(r"\b(però|ma|sebbene|perché|quando|a proposito)\b", re.IGNORECASE)
_CLAUSE_BREAK_TR = re.compile(r"\b(ancak|ama|fakat|çünkü|bu arada|ne zaman)\b", re.IGNORECASE)
_CLAUSE_BREAK_PL = re.compile(r"\b(jednak|ale|choć|ponieważ|kiedy|swoją drogą)\b", re.IGNORECASE)

_EL_CLIENT: ElevenLabsClient | None = None
_EMOJI_OR_SYMBOLS_ONLY = re.compile(r'^[\W_]+$', flags=re.UNICODE)

_LATIN_HINTS: list[tuple[str, re.Pattern]] = [
    ("es", re.compile(r"[áéíóúñÁÉÍÓÚÑ]")),
    ("pt", re.compile(r"[áâàãçéêíóôõúÁÂÀÃÇÉÊÍÓÔÕÚ]")),
    ("fr", re.compile(r"[àâçéèêëîïôûùÿœæÀÂÇÉÈÊËÎÏÔÛÙŸŒÆ]")),
    ("de", re.compile(r"[äöüÄÖÜß]")),
    ("it", re.compile(r"[àèéìíîòóùúÀÈÉÌÍÎÒÓÙÚ]")),
    ("tr", re.compile(r"[ğıüşöçİĞÜŞÖÇ]")),
    ("pl", re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")),
    ("cs", re.compile(r"[áéíóúýčďěňřšťžÁÉÍÓÚÝČĎĚŇŘŠŤŽ]")),
    ("sk", re.compile(r"[áäčďéíĺľňóôŕšťúýžÁÄČĎÉÍĹĽŇÓÔŔŠŤÚÝŽ]")),
    ("ro", re.compile(r"[ăâîșțĂÂÎȘȚ]")),
    ("nl", re.compile(r"[ïëáéíóúäöÁÉÍÓÚÄÖÏË]")),
    ("sv", re.compile(r"[åäöÅÄÖ]")),
    ("da", re.compile(r"[æøåÆØÅ]")),
    ("no", re.compile(r"[æøåÆØÅ]")),
    ("fi", re.compile(r"[åäöÅÄÖ]")),
    ("hu", re.compile(r"[áéíóöőúüűÁÉÍÓÖŐÚÜŰ]")),
    ("vi", re.compile(r"[ăâêôơưđĂÂÊÔƠƯĐ]")),
]

# ── SSML helpers / limits
_SSML_BREAK_MAX_MID_MS = int(getattr(settings, "SSML_BREAK_MAX_MID_MS", 1200))
_SSML_BREAK_MAX_ANY_MS = int(getattr(settings, "SSML_BREAK_MAX_ANY_MS",  3000))
_SSML_BREAKS_PER_100CH = int(getattr(settings, "SSML_BREAKS_PER_100CH",  1))

# Punct sets (basic): alphabetic vs CJK vs others
_ALPHA_SOFT = ",;:"
_ALPHA_HARD = ".?!"
_ALPHA_LONG = "—…"
_CJK_SOFT   = "、，；："
_CJK_HARD   = "。？！"
_SENT_END_RX = re.compile(r"[.!?…]|[。？！]")

def _is_short_single_sentence(s: str) -> bool:
    t = strip_markup((s or "")).strip().replace("...", "…")
    if not t:
        return True
    return (len(t) <= 100) and (len(_SENT_END_RX.findall(t)) <= 1)

# safe tag split regex
_SPLIT_TAGS_RX = re.compile(r"(<[^>]+>)")

_URL_RX    = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_HANDLE_RX = re.compile(r"(?<!\w)@[\w.]+")
_HASH_RX   = re.compile(r"(?<!\w)#[\w\-_]+")
_RU_ABBR = [
    (r"\bт\.к\.\b", "так как"),
    (r"\bт\.е\.\b", "то есть"),
    (r"\bв т\.ч\.\b", "в том числе"),
    (r"\bи т\.д\.\b", "и так далее"),
    (r"\bи т\.п\.\b", "и тому подобное"),
]
_EMOJI_MAP_RU = {"😂":"ха-ха","🤣":"ха-ха","🙂":"улыбка","😉":"улыбка","😊":"улыбка","😅":"хм","🤔":"хм","😮":"о","😢":"вздох","😡":"эм...","❤️":"любовь","🔥":"ого"}
_EMOJI_MAP_EN = {"😂":"ha-ha","🤣":"ha-ha","🙂":"smile","😉":"smile","😊":"smile","😅":"hmm","🤔":"hmm","😮":"oh","😢":"sigh","😡":"uh","❤️":"love","🔥":"wow"}
_INTERJ_RU = re.compile(r"^(ну|ладно|короче|кстати|слушай|смотри)\b", re.IGNORECASE)
_EMOJI_MAP_ES = {"😂":"ja-ja","🤣":"ja-ja","🙂":"sonrisa","😉":"sonrisa","😊":"sonrisa","😅":"hmm","🤔":"hmm","😮":"oh","😢":"suspiro","😡":"ejem","❤️":"amor","🔥":"wow"}
_EMOJI_MAP_PT = {"😂":"há-há","🤣":"há-há","🙂":"sorriso","😉":"sorriso","😊":"sorriso","😅":"hmm","🤔":"hmm","😮":"oh","😢":"suspiro","😡":"hã","❤️":"amor","🔥":"uau"}
_EMOJI_MAP_FR = {"😂":"ha-ha","🤣":"ha-ha","🙂":"sourire","😉":"sourire","😊":"sourire","😅":"hmm","🤔":"hmm","😮":"oh","😢":"soupir","😡":"euh","❤️":"amour","🔥":"waouh"}
_EMOJI_MAP_DE = {"😂":"ha-ha","🤣":"ha-ha","🙂":"lächeln","😉":"lächeln","😊":"lächeln","😅":"hmm","🤔":"hmm","😮":"oh","😢":"seufz","😡":"äh","❤️":"liebe","🔥":"wow"}
_EMOJI_MAP_IT = {"😂":"ah-ah","🤣":"ah-ah","🙂":"sorriso","😉":"sorriso","😊":"sorriso","😅":"hmm","🤔":"hmm","😮":"oh","😢":"sospiro","😡":"ehm","❤️":"amore","🔥":"wow"}
_NEUTRAL_SMILES = {"🙂","😉","😊"}

_EMAIL_RX = re.compile(r"\b[\w\.-]+@[\w\.-]+\.\w+\b")
_PHONE_RX = re.compile(r"\+?\d[\d\-\s()]{6,}\d")

_I18N_TOKENS = {
    "ru": {"url":"ссылка", "email":"почта", "phone":"номер"},
    "uk": {"url":"посилання", "email":"пошта", "phone":"номер"},
    "pl": {"url":"link", "email":"email", "phone":"numer"},
    "es": {"url":"enlace", "email":"correo", "phone":"número"},
    "pt": {"url":"link", "email":"email", "phone":"número"},
    "fr": {"url":"lien", "email":"email", "phone":"numéro"},
    "de": {"url":"link", "email":"email", "phone":"nummer"},
    "it": {"url":"link", "email":"email", "phone":"numero"},
    "tr": {"url":"bağlantı", "email":"e-posta", "phone":"numara"},
    "nl": {"url":"link", "email":"e-mail", "phone":"nummer"},
    "sv": {"url":"länk", "email":"e-post", "phone":"nummer"},
    "da": {"url":"link", "email":"e-mail", "phone":"nummer"},
    "no": {"url":"lenke", "email":"e-post", "phone":"nummer"},
    "fi": {"url":"linkki", "email":"sähköposti", "phone":"numero"},
    "ro": {"url":"link", "email":"email", "phone":"număr"},
    "hu": {"url":"link", "email":"email", "phone":"szám"},
    "en": {"url":"link", "email":"email", "phone":"number"},
}

_ABBR_EN = [(r"\bi\.e\.\b","that is"),(r"\be\.g\.\b","for example"),(r"\betc\.\b","et cetera")]
_ABBR_ES = [(r"\bp\.\s?ej\.\b","por ejemplo"),(r"\betc\.\b","etcétera")]
_ABBR_PT = [(r"\bp\.\s?ex\.\b","por exemplo")]
_ABBR_FR = [(r"\bp\.\s?ex\.\b","par exemple"),(r"\bc\.-à-d\.\b","c’est-à-dire")]
_ABBR_DE = [(r"\bz\.\s?B\.\b","zum Beispiel"),(r"\bd\.\s?h\.\b","das heißt")]
_ABBR_IT = [(r"\bp\.?\s?es\.\b","per esempio")]
_ABBR_PL = [(r"\bnp\.\b","na przykład"),(r"\btzn\.\b","to znaczy")]

_PHONEME_REDIS_KEY = getattr(settings, "PHONEME_REDIS_DICT_KEY", "pron:en")
_PHONEME_MAX_PER_MSG = int(getattr(settings, "PHONEME_MAX_PER_MESSAGE", 6))

_CB_FAILS = int(getattr(settings, "PERSONAL_PING_TTS_CB_FAILS", 3))
_CB_COOLDOWN_SEC = int(getattr(settings, "PERSONAL_PING_TTS_CB_COOLDOWN_SEC", 3600))
_CB_DISABLE_UNTIL_KEY = "tts:cb:disable_until:{chat_id}"
_CB_FAILCOUNT_KEY     = "tts:cb:failcount:{chat_id}"

def l10n(lang: str) -> dict[str,str]:
    l = (lang or "en").split("-")[0].lower()
    return _I18N_TOKENS.get(l, _I18N_TOKENS["en"])

async def _get_el_client() -> ElevenLabsClient:
    global _EL_CLIENT
    if _EL_CLIENT is None:
        _EL_CLIENT = ElevenLabsClient.from_settings()
    return _EL_CLIENT

async def shutdown_tts() -> None:

    global _EL_CLIENT
    try:
        if _EL_CLIENT is not None:
            await _EL_CLIENT.close()
    except Exception:
        logger.debug("shutdown_tts: ignore close error", exc_info=True)
    finally:
        _EL_CLIENT = None

def atexit_shutdown():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(shutdown_tts())
    except Exception:
        pass

atexit.register(atexit_shutdown)

def detect_lang(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return "en"
    if "¿" in t or "¡" in t:
        return "es"
    if _CYR_RX.search(t):
        return "uk" if re.search(r"[ЄєЇїІіҐґ]", t) else "ru"
    if _KO_RX.search(t):
        return "ko"
    if _JA_RX.search(t):
        return "ja"
    if _AR_RX.search(t):
        return "ar"
    if _HEB_RX.search(t):
        return "he"
    if _DEV_RX.search(t):
        return "hi"
    if _EL_RX.search(t):
        return "el"
    if _TH_RX.search(t):
        return "th"
    if _CJK_RX.search(t):
        if _JA_PUNCT_RX.search(t):
            return "ja"
        return "zh"
    for code, rx in _LATIN_HINTS:
        if rx.search(t):
            return code
    tl = " " + re.sub(r"\s+", " ", t.lower()) + " "
    if any(w in tl for w in (" el ", " la ", " y ", " que ", " por ")):
        return "es"
    if any(w in tl for w in (" le ", " la ", " et ", " est ", " que ")):
        return "fr"
    if any(w in tl for w in (" und ", " der ", " die ", " das ", " weil ")):
        return "de"
    if any(w in tl for w in (" e ", " il ", " la ", " che ", " per ")):
        return "it"
    if any(w in tl for w in (" e ", " o ", " que ", " para ")):
        return "pt"
    return "en"

def has_script_markers(t: str) -> bool:
    return any(rx.search(t) for rx in (_CYR_RX, _KO_RX, _JA_RX, _AR_RX, _HEB_RX, _CJK_RX))

async def get_user_lang(user_id: int, fallback_text: str, user_text_hint: str | None = None) -> str:
    hint = (user_text_hint or "").strip()
    if len(hint) >= LANG_DETECT_HINT_MIN:
        detected = detect_lang(hint)
        if detected:
            return detected

    if hint and len(hint) < LANG_DETECT_HINT_MIN and has_script_markers(hint):
        detected_short = detect_lang(hint)
        if detected_short:
            return detected_short

    try:
        raw = await redis_client.get(LANG_TTS_REDIS_KEY_FMT.format(user_id=user_id))
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "ignore")
        lang = (raw or "").strip()
        if lang:
            return lang
    except Exception:
        logger.debug("Failed to read TTS lang from redis", exc_info=True)

    try:
        raw_ui = await redis_client.get(LANG_UI_REDIS_KEY_FMT.format(user_id=user_id))
        if not raw_ui:
            raw_ui = await redis_client.get(f"lang_ui:{user_id}")
        if isinstance(raw_ui, (bytes, bytearray)):
            raw_ui = raw_ui.decode("utf-8", "ignore")
        lang_ui = (raw_ui or "").strip()
        if lang_ui:
            try:
                await redis_client.setex(LANG_TTS_REDIS_KEY_FMT.format(user_id=user_id), 7*24*3600, lang_ui)
            except Exception:
                logger.debug("Failed to cache UI lang into TTS cache", exc_info=True)
            return lang_ui
    except Exception:
        logger.debug("Failed to read UI lang from redis", exc_info=True)
    detected = detect_lang(fallback_text)
    lang_final = detected or "en"

    try:
        await redis_client.setex(LANG_TTS_REDIS_KEY_FMT.format(user_id=user_id), 7*24*3600, lang_final)
    except Exception:
        logger.debug("Failed to cache TTS lang to redis", exc_info=True)
    return lang_final

_TAG_RX = re.compile(r"<[^>]+>")

def strip_markup(t: str) -> str:
    return _TAG_RX.sub("", (t or "")).strip()

def is_tts_eligible_short(text: str) -> bool:
    t = strip_markup(text or "")
    if not t:
        return False
    t_norm = t.replace("...", "…")
    s_cnt = len(_SENT_END_RX.findall(t_norm))
    if s_cnt == 0 and t_norm:
        s_cnt = 1
    return (len(t_norm) <= MAX_TTS_CHARS_STRICT) and (s_cnt <= MAX_TTS_SENTENCES)

def maybe_expand_abbr(t: str, lang: str) -> str:
    l = (lang or "en").split("-")[0].lower()
    tables = {"en":_ABBR_EN,"es":_ABBR_ES,"pt":_ABBR_PT,"fr":_ABBR_FR,"de":_ABBR_DE,"it":_ABBR_IT,"pl":_ABBR_PL,"ru":_RU_ABBR}
    for rx_tbl in tables.get(l, []):
        t = re.sub(rx_tbl[0], rx_tbl[1], t, flags=re.IGNORECASE)
    return t

def insert_soft_pauses(t: str, lang: str) -> str:

    s = t
    l = (lang or "en").split("-")[0].lower()
    if l in ("ru","uk","be"):
        s = _CLAUSE_BREAK_RU.sub(lambda m: (", " if m.start() > 0 else "") + m.group(1), s)
    elif l == "es":
        s = _CLAUSE_BREAK_ES.sub(lambda m: ", " + m.group(1), s)
    elif l == "pt":
        s = _CLAUSE_BREAK_PT.sub(lambda m: ", " + m.group(1), s)
    elif l == "fr":
        s = _CLAUSE_BREAK_FR.sub(lambda m: ", " + m.group(1), s)
    elif l == "de":
        s = _CLAUSE_BREAK_DE.sub(lambda m: ", " + m.group(1), s)
    elif l == "it":
        s = _CLAUSE_BREAK_IT.sub(lambda m: ", " + m.group(1), s)
    elif l == "tr":
        s = _CLAUSE_BREAK_TR.sub(lambda m: ", " + m.group(1), s)
    elif l == "pl":
        s = _CLAUSE_BREAK_PL.sub(lambda m: ", " + m.group(1), s)
    else:
        s = _CLAUSE_BREAK_EN.sub(lambda m: ", " + m.group(1), s)

    if len(re.findall(r"\S+", s)) >= 14:
        s = re.sub(r"((?:\S+\s+){12,18}\S+)", r"\1 —", s, count=1)
    return s

def trim_for_tts(text: str) -> str:

    t = strip_markup(text)
    if len(t) <= MAX_TTS_CHARS:
        return t

    cut = t[:MAX_TTS_CHARS]

    tail_match = re.search(r"[.!?…][^.!?…]{0,120}\Z", cut)
    if tail_match:
        return cut[:tail_match.end()]

    p = cut.rfind(" ")
    return (cut[:p] if p > 200 else cut) + "…"

def break_dur_ms(ch: str, lang: str) -> int:
    
    l = (lang or "en").split("-")[0].lower()
    is_cjk = l in ("zh","ja","ko")

    if is_cjk:
        if ch in _CJK_HARD:
            return 360
        if ch in _CJK_SOFT:
            return 180

    if ch in _ALPHA_HARD:
        if ch == ".":
            return 360
        if ch == "?":
            return 420
        if ch == "!":
            return 450
        return 380

    if ch == "—":
        return 300
    if ch == "…":
        return 650

    if ch in _ALPHA_SOFT:
        return 200

    return 0

def scale_break_ms(ms: int, emo: Optional[Dict[str,float]]) -> int:
    if not emo:
        return ms
    a = float(emo.get("arousal", 0.5))
    if a >= 0.65:
        k = 0.75
    elif a <= 0.35:
        k = 1.25
    else:
        k = 1.0
    out = int(ms * k)
    out = max(0, min(out, _SSML_BREAK_MAX_ANY_MS))
    return out

def limit_breaks_budget(text_len: int) -> int:
    return 9999

def inject_ssml_breaks(plain: str, lang: str, emo: Optional[Dict[str,float]]) -> str:
    if not plain:
        return plain

    def xml_escape_char(ch: str) -> str:
        if ch == "&": return "&amp;"
        if ch == "<": return "&lt;"
        if ch == ">": return "&gt;"
        return ch

    MAX_BREAKS = 16
    hard = set(_ALPHA_HARD + "…")
    soft = set(_ALPHA_SOFT + "—")
    closers = set('»”")]}’\'')
    total_candidates = sum(1 for c in plain if c in hard or c in soft)
    budget = min(MAX_BREAKS, max(3, total_candidates))

    out, used = [], 0
    n = len(plain)

    for i, ch in enumerate(plain):
        out.append(xml_escape_char(ch))
        if used >= budget:
            continue
        if ch not in hard and ch not in soft:
            continue
        if i == n - 1:
            continue

        ms = break_dur_ms(ch, lang)
        if ms <= 0:
            continue

        if (i + 1 < n) and (plain[i+1] in hard):
            continue

        j = i + 1
        while j < n and plain[j].isspace():
            j += 1
        if j < n and plain[j] in closers:
            continue

        ms = scale_break_ms(min(ms, _SSML_BREAK_MAX_MID_MS), emo)
        if ms <= 0:
            continue

        out.append(f'<break time="{ms}ms"/>')
        used += 1

    return "".join(out)

def maybe_wrap_ssml(s: str) -> str:

    x = (s or "").strip()
    if not x:
        return x
    low = x.lower()
    if low.startswith("<speak") and low.endswith("</speak>"):
        return x
    if "<break" in low or "<phoneme" in low:
        return f"<speak>{x}</speak>"
    return x

async def apply_phonemes_en_ssml(ssml: str) -> str:
    if not PHONEMES_EN:
        return ssml

    parts = _SPLIT_TAGS_RX.split(ssml)

    rx_tok = re.compile(r"\b([A-Z]{2,7}|[A-Z][a-z]+(?:[A-Z][a-z]+)?)\b")
    replaced = 0
    for i, seg in enumerate(parts):
        if not seg or seg.startswith("<"):
            continue
        async def _sub_async(m):
            nonlocal replaced
            if replaced >= _PHONEME_MAX_PER_MSG:
                return m.group(0)
            tok = m.group(1)
            key = tok.lower()
            ipa = None
            try:
                raw = None
                with contextlib.suppress(Exception):
                    raw = await redis_client.hget(_PHONEME_REDIS_KEY, key)
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", "ignore")
                if raw:
                    if raw.startswith("ipa:"):
                        ipa = raw[4:]
                    elif raw.startswith("arpabet:"):
                        ipa = None
                if ipa:
                    replaced += 1
                    return f'<phoneme alphabet="ipa" ph="{ipa}">{tok}</phoneme>'
            except Exception:
                pass
            return m.group(0)
        out = []
        last = 0
        for m in rx_tok.finditer(seg):
            out.append(seg[last:m.start()])
            out.append(await _sub_async(m))
            last = m.end()
        out.append(seg[last:])
        parts[i] = "".join(out)
    return "".join(parts)

def infer_style_from_text(text: str) -> Dict[str, float]:

    t = strip_markup((text or "")).strip()

    exclam = t.count("!")
    qmarks = t.count("?")
    emojis = len(_EMOJI_RX.findall(t))
    caps = sum(1 for w in re.findall(r"\b[A-Z]{2,}\b", t)) if re.search(r"[A-Za-z]", t) else 0
    length = len(t)

    excitement = min(1.0, 0.13*exclam + 0.08*qmarks + 0.06*emojis + 0.04*caps + 0.0003*length)

    stability = max(0.45, min(0.92, 0.88 - 0.45*excitement))
    style =     max(0.18, min(0.70, 0.22 + 0.55*excitement))
    similarity_boost = max(0.60, min(0.92, 0.70 + 0.20*(1.0 - excitement)))

    return {
        "stability": float(round(stability, 3)),
        "similarity_boost": float(round(similarity_boost, 3)),
        "style": float(round(style, 3)),
        "use_speaker_boost": True,
    }

def map_emojis(text: str, lang: str) -> str:
    for e in _NEUTRAL_SMILES:
        text = text.replace(e, " … ")
    if lang.lower().startswith(("ru","uk","be")):
        for e, w in _EMOJI_MAP_RU.items():
            text = text.replace(e, f" {w} ")
        return text
    l = (lang or "en").split("-")[0].lower()
    if l in ("en",):
        for e, w in _EMOJI_MAP_EN.items():
            text = text.replace(e, f" {w} ")
        return text
    if l == "es":
        for e, w in _EMOJI_MAP_ES.items():
            text = text.replace(e, f" {w} ")
        return text
    if l == "pt":
        for e, w in _EMOJI_MAP_PT.items():
            text = text.replace(e, f" {w} ")
        return text
    if l == "fr":
        for e, w in _EMOJI_MAP_FR.items():
            text = text.replace(e, f" {w} ")
        return text
    if l == "de":
        for e, w in _EMOJI_MAP_DE.items():
            text = text.replace(e, f" {w} ")
        return text
    if l == "it":
        for e, w in _EMOJI_MAP_IT.items():
            text = text.replace(e, f" {w} ")
        return text
    return _EMOJI_RX.sub(" … ", text)

def expand_ru_abbr(t: str) -> str:
    out = t
    for rx, repl in _RU_ABBR:
        out = re.sub(rx, repl, out, flags=re.IGNORECASE)
    return out

def normalize_punct(t: str, lang: str) -> str:
    s = re.sub(r"\s+", " ", t).strip()
    s = re.sub(r"!{3,}", "!!", s)
    s = re.sub(r"\?{3,}", "??", s)
    s = s.replace("...", "…")
    l = (lang or "en").split("-")[0].lower()
    if l in ("ru","uk","be"):
        s = re.sub(_INTERJ_RU, lambda m: m.group(0).capitalize() + ",", s)
        s = re.sub(r'(^|[\s(])"([^"]+)"', r'\1«\2»', s)
        s = s.replace("'", "’")

    s = re.sub(r'^\s*,\s*', '', s)

    s = re.sub(r"\s+([,.?!…])", r"\1", s)

    s = re.sub(r"[,:;—-]+\s*([.?!…])\s*$", r"\1", s)

    s = re.sub(r"[,:;—-]+\s*$", ".", s)

    s = re.sub(r"([.?!…])\1{1,}", r"\1", s)
    s = s.replace("….", "…").replace("!.", "!").replace("?.", "?")

    if s and s[-1] not in ".?!…":
        s += "."
    return s


def preprocess_for_tts(text: str, lang: str, *, allow_markup: bool = False) -> str:
    t = text if allow_markup else strip_markup(text)
    loc = l10n(lang)
    t = _URL_RX.sub(f" {loc['url']} ", t)
    t = _HANDLE_RX.sub("", t)
    t = _HASH_RX.sub("", t)
    t = _EMAIL_RX.sub(f" {loc['email']} ", t)
    t = _PHONE_RX.sub(f" {loc['phone']} ", t)
    t = re.sub(r"\s+", " ", t).strip().replace("...", "…")
    if SSML_MODE == "off":
        if t and t[-1] not in ".?!…":
            t += "."
        return t
    t = map_emojis(t, lang)
    t = maybe_expand_abbr(t, lang)
    t = insert_soft_pauses(t, lang)
    t = normalize_punct(t, lang)
    return t

_DIGITISH_RX = re.compile(r"(?:\d[%€$]?)|(?:\b\d{1,2}:\d{2}\b)|(?:\b\d+[.,]\d+\b)")
_ABBRISH_RX  = re.compile(r"\b(?:ETA|CPU|GPU|API|SDK|AI|ML|NLP)\b", re.IGNORECASE)

def needs_strict_normalization(t: str) -> bool:
    digits = len(re.findall(r"\d", t))
    return bool(_DIGITISH_RX.search(t) or _ABBRISH_RX.search(t) or digits >= max(5, len(t)//12))

def calibrate_voice_settings(vs: Dict[str, Any], lang: str) -> Dict[str, Any]:
    def clamp(x, lo, hi): return float(max(lo, min(hi, x)))
    s  = clamp(float(vs.get("stability", 0.70)), 0.65, 0.93)
    st = clamp(float(vs.get("style",     0.25)), 0.14, 0.42)
    sim= clamp(float(vs.get("similarity_boost", 0.62)), 0.50, 0.80)
    l = (lang or "en").split("-")[0].lower()
    if l in ("ru","uk","be","pl"):
        s = clamp(s + 0.01, 0.65, 0.93)
    if l in ("zh","ja","ko","vi"):
        st = clamp(st - 0.02, 0.14, 0.42)
        s  = clamp(s + 0.02,  0.65, 0.93)
    if l in ("ar","he"):
        s = clamp(s + 0.01,  0.65, 0.93)
    if l in ("es","pt","fr","it","de","tr"):
        s  = clamp(s + 0.01,  0.65, 0.93)
        st = clamp(st - 0.01,  0.14, 0.42)
    return {"stability": round(s,3), "style": round(st,3), "similarity_boost": round(sim,3), "use_speaker_boost": bool(vs.get("use_speaker_boost", True))}

def infer_style_from_emotions(text: str, emo: Optional[Dict[str, float]]) -> Dict[str, float]:

    tp = strip_markup(text or "")
    exclam = tp.count("!")
    qmarks = tp.count("?")
    emojis = len(_EMOJI_RX.findall(tp))
    caps = sum(1 for w in re.findall(r"\b[A-Z]{2,}\b", tp)) if re.search(r"[A-Za-z]", tp) else 0
    length = len(tp)
    excite_txt = min(1.0, 0.12*exclam + 0.08*qmarks + 0.06*emojis + 0.04*caps + 0.0003*length)

    def clamp01(x: float) -> float:
        return max(0.0, min(1.0, float(x)))

    if not emo:
        return infer_style_from_text(text)

    arousal  = clamp01(emo.get("arousal", 0.5))
    energy   = clamp01(emo.get("energy", 0.5))
    valence  = float(emo.get("valence", 0.0))          # [-1..1]
    pos01    = clamp01((valence + 1.0) * 0.5)         # → [0..1]
    stress   = clamp01(emo.get("stress", 0.0))
    anxiety  = clamp01(emo.get("anxiety", 0.0))
    anger    = clamp01(emo.get("anger", 0.0))
    sadness  = clamp01(emo.get("sadness", 0.0))

    excite_emo = clamp01(0.60*arousal + 0.40*energy)
    tension    = max(stress, anxiety, anger)
    excite     = clamp01(0.65*excite_emo + 0.35*excite_txt)

    neutrality_penalty = max(0.0, 0.25 - abs(valence))
    stability = max(0.50, min(0.95, 0.92 - 0.60*excite - 0.30*tension + 0.06*pos01))
    style     = max(0.18, min(0.70, 0.20 + 0.50*excite + 0.00*(pos01 - 0.5) - 0.08*neutrality_penalty))
    sim_boost = max(0.60, min(0.95, 0.74 + 0.12*(1.0 - tension) - 0.08*excite))

    return {
        "stability": float(round(stability, 3)),
        "similarity_boost": float(round(sim_boost, 3)),
        "style": float(round(style, 3)),
        "use_speaker_boost": True,
    }

def text_excitement_quick(t: str) -> float:
    t = strip_markup(t or "")
    exclam = t.count("!")
    qmarks = t.count("?")
    emojis = len(_EMOJI_RX.findall(t))
    caps = sum(1 for w in re.findall(r"\b[A-Z]{2,}\b", t)) if re.search(r"[A-Za-z]", t) else 0
    length = len(t)
    return min(1.0, 0.12*exclam + 0.08*qmarks + 0.06*emojis + 0.04*caps + 0.0003*length)

def apply_tone_governor(vs: Dict[str, Any], text: str, emo: Optional[Dict[str, float]]) -> Dict[str, Any]:
    
    out = dict(vs)
    t = strip_markup(text or "")
    has_peak = ("!" in t) or ("?" in t)
    has_ellipsis = ("…" in t) or ("..." in t)
    has_longdash = ("—" in t)
    excite = text_excitement_quick(t)
    val = float(emo.get("valence", 0.0)) if emo else 0.0
    ar = float(emo.get("arousal", 0.5)) if emo else 0.5
    en = float(emo.get("energy", 0.5))  if emo else 0.5
    excite_emo = 0.60*ar + 0.40*en
    neutralish = (abs(val) < 0.12) and (0.35 <= excite_emo <= 0.55) and (excite < 0.25)

    if neutralish:
        out["stability"] = max(float(out.get("stability", 0.7)), 0.84)
        out["style"]     = min(float(out.get("style", 0.25)),     0.22)
        out["use_speaker_boost"] = False

    if val > 0.05:
        damp_style   = min(0.08, 0.04 + 0.06*val)
        boost_stable = min(0.06, 0.03 + 0.05*val)
        out["style"]     = max(0.18, float(out.get("style", 0.3)) - damp_style)
        out["stability"] = min(0.95, float(out.get("stability", 0.6)) + boost_stable)
        out["use_speaker_boost"] = False

    if (has_ellipsis or has_longdash) and (excite < 0.35):
        out["stability"] = max(float(out.get("stability", 0.7)), 0.86)
        out["style"]     = min(float(out.get("style", 0.25)),    0.24)

    if (val < -0.25) and (excite_emo < 0.45):
        out["stability"] = max(float(out.get("stability", 0.6)), 0.80)
        out["style"]     = min(float(out.get("style", 0.3)),     0.26)

    if (val > 0.35) and (excite < 0.30):
        out["style"] = min(float(out.get("style", 0.3)), 0.34)

    out["stability"] = float(max(0.65, min(0.95, out.get("stability", 0.75))))
    max_style_cap = 0.42 if has_peak else 0.36
    out["style"] = float(max(0.14, min(max_style_cap, out.get("style", 0.25))))
    out["similarity_boost"] = float(max(0.50, min(0.80, out.get("similarity_boost", 0.62))))
    return out

def blend_voice_settings(a: Dict[str, Any], b: Dict[str, Any], alpha: float) -> Dict[str, Any]:
    def f(k, lo=0.0, hi=1.0):
        return float(round(max(lo, min(hi, (1.0-alpha)*float(a.get(k, 0.5)) + alpha*float(b.get(k, 0.5)))), 3))
    return {
        "stability": f("stability"),
        "similarity_boost": f("similarity_boost"),
        "style": f("style"),
        "use_speaker_boost": bool(a.get("use_speaker_boost", True) and b.get("use_speaker_boost", True)),
    }

def has_letters(s: str) -> bool:

    try:
        return any(unicodedata.category(ch).startswith("L") for ch in s)
    except Exception:
        return has_script_markers(s)

def looks_like_only_symbols(text: str) -> bool:

    t = (text or "").strip()
    if not t:
        return True

    if t.isdigit() and len(t) < 6:
        return True

    if re.search(r"[0-9A-Za-z]", t, flags=re.UNICODE):
        return False

    if has_letters(t) or has_script_markers(t):
        return False
    return bool(_EMOJI_OR_SYMBOLS_ONLY.match(t))

async def choose_voice(user_id: int, lang: str, override_voice_id: Optional[str]) -> Tuple[ElevenLabsClient, Optional[str]]:
    client = await _get_el_client()
    voice_id = await client.pick_voice(user_id=user_id, lang=lang, override_voice_id=override_voice_id)
    if not voice_id:
        fallback_map = {
            "uk": "ru",
            "be": "ru",
            "zh-cn": "zh",
            "zh-hans": "zh",
            "zh-hant": "zh",
        }
        fb_lang = fallback_map.get(lang.lower())
        if fb_lang and fb_lang != lang.lower():
            try:
                voice_id = await client.pick_voice(user_id=user_id, lang=fb_lang, override_voice_id=override_voice_id)
            except Exception:
                voice_id = None
        if not voice_id:
            try:
                voice_id = await client.pick_voice(user_id=user_id, lang="multi", override_voice_id=override_voice_id)
            except Exception:
                voice_id = None
        if not voice_id and lang.lower() != "en":
            try:
                voice_id = await client.pick_voice(user_id=user_id, lang="en", override_voice_id=override_voice_id)
            except Exception:
                voice_id = None
    return client, voice_id

async def generate_voice_for_reply(
    *,
    reply_text: str,
    user_id: int,
    chat_id: int,
    voice_in: bool = False,
    override_voice_id: Optional[str] = None,
    force: bool = False,
    user_text_hint: Optional[str] = None,
    ) -> Optional[str]:

    persona = None
    emo_snapshot: Optional[Dict[str, float]] = None
    if TTS_USE_EMOTIONS:
        try:
            p = await get_persona(chat_id=chat_id, user_id=user_id)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(p.ready(0.50), timeout=0.60)
            persona = p
            emo_snapshot = {
                "arousal":  float(getattr(p, "ema", {}).get("arousal", p.state.get("arousal", 0.5))),
                "energy":   float(getattr(p, "ema", {}).get("energy",  p.state.get("energy",  0.5))),
                "valence":  float(getattr(p, "ema", {}).get("valence", p.state.get("valence", 0.0))),
                "stress":   float(p.state.get("stress",   0.0)),
                "anxiety":  float(p.state.get("anxiety",  0.0)),
                "anger":    float(p.state.get("anger",    0.0)),
                "sadness":  float(p.state.get("sadness",  0.0)),
            }
        except Exception:
            persona = None
            emo_snapshot = None

    if not TTS_ENABLED:
        return None

    should_speak = bool(force)
    if not should_speak:
        return None

    if not is_tts_eligible_short(reply_text):
        return None

    lang = await get_user_lang(user_id, reply_text, user_text_hint=user_text_hint)
    try:
        plain_prepared = preprocess_for_tts((reply_text or "").strip(), lang, allow_markup=False)
    except Exception:
        plain_prepared = (reply_text or "").strip()

    trimmed_plain = trim_for_tts(plain_prepared)
    text = trimmed_plain

    want_ssml = SSML_ENABLE and (SSML_MODE in ("on","auto"))
    if want_ssml and _is_short_single_sentence(trimmed_plain):
        if not re.search(r"[,;:—…]", trimmed_plain):
            want_ssml = False

    ssml_text: Optional[str] = None
    emo_for_breaks: Optional[Dict[str,float]] = emo_snapshot if TTS_USE_EMOTIONS else None

    if want_ssml:
        try:
            ssml = inject_ssml_breaks(trimmed_plain, lang, emo_for_breaks)
            if PHONEMES_EN and (lang or "en").split("-")[0].lower() == "en":
                ssml = await apply_phonemes_en_ssml(ssml)
            ssml_text = maybe_wrap_ssml(ssml)
            text = ssml_text
        except Exception:
            ssml_text = None
            text = trimmed_plain

    if not text.strip():
        return None

    try:
        client, voice_id = await choose_voice(user_id, lang, override_voice_id)
        if not voice_id:
            logger.info("No voice_id resolved for lang=%s; skip TTS", lang)
            return None

        base_vs = infer_style_from_text(text)

        emo_vs = None
        if TTS_USE_EMOTIONS:
            emo_vs = infer_style_from_emotions(text, emo_snapshot)

        base_vs = calibrate_voice_settings(base_vs, lang)
        if emo_vs:
            emo_vs = calibrate_voice_settings(emo_vs, lang)

        blend_alpha = max(0.0, min(1.0, TTS_EMO_BLEND))
        if emo_snapshot:
            excite_emo = 0.60*float(emo_snapshot.get("arousal", 0.5)) + 0.40*float(emo_snapshot.get("energy", 0.5))
            if abs(float(emo_snapshot.get("valence", 0.0))) < 0.12 and 0.35 <= excite_emo <= 0.55:
                blend_alpha = min(blend_alpha, 0.40)
        voice_settings = (
            blend_voice_settings(base_vs, emo_vs, alpha=blend_alpha)
            if (TTS_USE_EMOTIONS and emo_vs) else base_vs
        )

        L = len(trimmed_plain)

        if L < 60:
            has_peak = any(ch in trimmed_plain for ch in "?!")
            voice_settings["stability"] = min(0.95, float(voice_settings.get("stability", 0.75)) + 0.06)
            if has_peak:
                voice_settings["style"] = min(0.40, float(voice_settings.get("style", 0.25)) + 0.02)
            else:
                voice_settings["style"] = min(float(voice_settings.get("style", 0.25)), 0.28)
        elif L > 300:
            voice_settings["stability"] = min(0.95, float(voice_settings.get("stability", 0.75)) + 0.04)

        tail = trimmed_plain.strip()[-1:] if trimmed_plain.strip() else "."
        l = (lang or "en").split("-")[0].lower()
        if tail == "?":
            dq = -0.02 if l in ("ru","uk","be") else -0.04
            sq = +0.02 if l in ("ru","uk","be") else +0.03
            voice_settings["stability"] = max(0.65, float(voice_settings.get("stability", 0.75)) + dq)
            voice_settings["style"]     = min(0.40, float(voice_settings.get("style", 0.25)) + sq)
        elif tail == "!":
            voice_settings["stability"] = max(0.65, float(voice_settings.get("stability", 0.75)) - 0.04)
            voice_settings["style"]     = min(0.42, float(voice_settings.get("style", 0.25)) + 0.04)
        elif tail == "…":
            voice_settings["stability"] = min(0.95, float(voice_settings.get("stability", 0.75)) + 0.04)
            voice_settings["style"]     = max(0.14, float(voice_settings.get("style", 0.25)) - 0.03)
        
        if tail == "." and text_excitement_quick(trimmed_plain) < 0.30:
            voice_settings["stability"] = max(float(voice_settings.get("stability", 0.75)), 0.88)
            voice_settings["style"] = min(float(voice_settings.get("style", 0.25)), 0.24)

        _seed_j = zlib.crc32((str(user_id) + "|" + trimmed_plain[-32:]).encode("utf-8")) & 0xFFFF
        _rng = random.Random(_seed_j)
        voice_settings["stability"] = float(min(0.95, max(0.30, voice_settings["stability"] + _rng.uniform(-0.015, 0.015))))
        voice_settings["style"]     = float(min(0.95, max(0.00, voice_settings["style"]     + _rng.uniform(-0.03, 0.03))))

        voice_settings = apply_tone_governor(voice_settings, trimmed_plain, emo_snapshot)

        apply_norm_cfg = getattr(settings, "ELEVENLABS_TTS_NORMALIZATION", "auto") or "auto"

        plain_for_norm = trimmed_plain
        expressive_line = any(ch in trimmed_plain for ch in "?!…")

        if ssml_text:
            apply_norm = "auto"
        else:
            l = (lang or "en").split("-")[0].lower()
            if l in ("ru","uk","be") or needs_strict_normalization(plain_for_norm) or expressive_line:
                apply_norm = "on"
            else:
                apply_norm = apply_norm_cfg

        seed_cfg = getattr(settings, "ELEVENLABS_SEED", None)
        seed = None
        if isinstance(seed_cfg, (int, str)) and str(seed_cfg).strip().isdigit():
            base = int(seed_cfg)
            jitter = zlib.crc32((str(user_id) + ":" + text[:80]).encode("utf-8")) & 0x7FFFFFFF
            seed = (base + jitter) % (2**31 - 1)
        else:
            seed = zlib.crc32((str(user_id) + "|" + text[:80]).encode("utf-8")) & 0x7FFFFFFF
        out_fmt = getattr(settings, "ELEVENLABS_OUTPUT_FORMAT", "ogg_48000") or "ogg_48000"

        async def _synth_once(vs: Dict[str, Any], sd: Optional[int]):
            return await asyncio.wait_for(
                client.synthesize(
                    text=text,
                    lang=lang,
                    voice_id=voice_id,
                    voice_settings=vs,
                    output_format=out_fmt,
                    apply_text_normalization=apply_norm,
                    seed=sd if isinstance(sd, int) else None,
                ),
                timeout=float(getattr(settings, "ELEVENLABS_TTS_TIMEOUT", getattr(settings, "ELEVENLABS_TIMEOUT", 25.0))),
            )

        def _looks_like_bad_ssml_error(msg: str) -> bool:
            m = msg.lower()
            return ("ssml" in m and ("invalid" in m or "unsupported" in m)) or ("malformed xml" in m)

        try:
            audio = await _synth_once(voice_settings, seed)
        except Exception as e:
            msg = str(e)
            if (SSML_MODE == "auto") and ssml_text and _looks_like_bad_ssml_error(msg):
                logger.warning("SSML rejected by TTS, falling back to punctuation mode")
                try:
                    def _strip_breaks(s: str) -> str:
                        return re.sub(r'<\s*break\b[^>]*>', "", s, flags=re.IGNORECASE)
                    fallback_plain = strip_markup(_strip_breaks(ssml_text))
                    text = trim_for_tts(fallback_plain)
                except Exception:
                    text = trimmed_plain
                audio = await _synth_once(voice_settings, (seed + 42) % (2**31 - 1) if isinstance(seed, int) else None)
            elif "HTTP 429" in msg or "HTTP 503" in msg:
                vs2 = dict(voice_settings)
                vs2["stability"] = min(0.95, float(vs2.get("stability", 0.6)) + 0.05)
                vs2["style"]     = max(0.00, float(vs2.get("style", 0.3)) - 0.05)
                seed2 = (seed + 1337) % (2**31 - 1) if isinstance(seed, int) else None
                audio = await _synth_once(vs2, seed2)
            else:
                raise

        def looks_like_ogg_opus(buf: bytes) -> bool:
            return (len(buf) > 64) and (buf[:4] == b'OggS') and (b'OpusHead' in buf[:128])

        async def reencode_to_ogg_opus(src_bytes: bytes) -> Optional[str]:
            in_fd, in_path = tempfile.mkstemp(suffix=".bin", prefix="tts_in_")
            os.close(in_fd)
            with open(in_path, "wb") as _f:
                _f.write(src_bytes)
            out_fd, out_path = tempfile.mkstemp(suffix=".ogg", prefix="tts_")
            os.close(out_fd)
            try:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", in_path, "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "48k",
                        out_path, stdout=asp.DEVNULL, stderr=asp.DEVNULL
                    )
                except FileNotFoundError:
                    logger.warning("TTS: ffmpeg not found — cannot reencode to Ogg/Opus")
                    return None
                try:
                    await asyncio.wait_for(proc.wait(), timeout=float(getattr(settings, "FFMPEG_REENCODE_TIMEOUT", 15)))
                except asyncio.TimeoutError:
                    with contextlib.suppress(Exception):
                        proc.kill()
                    return None
                if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    return out_path
                return None
            finally:
                with contextlib.suppress(Exception):
                    os.remove(in_path)

        if not looks_like_ogg_opus(audio):
            fixed = await reencode_to_ogg_opus(audio)
            if not fixed:
                logger.warning("TTS: failed to reencode non-Ogg/Opus audio — skipping voice send")
                return None
            return fixed

        fd, tmp_path = tempfile.mkstemp(suffix=".ogg", prefix="tts_")
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        return tmp_path
    except Exception as e:
        logger.exception("TTS generation failed: %s", e)
        return None

def will_speak(*, voice_in: bool, force: bool = False) -> bool:

    if not TTS_ENABLED:
        return False
    if force:
        return True
    if voice_in:
        return (random.random() < TTS_PROB_VOICEIN)
    return (random.random() < TTS_PROB_TEXT)

async def maybe_tts_and_send(
    chat_id: int,
    user_id: int,
    reply_text: str,
    *,
    voice_in: bool = False,
    override_voice_id: Optional[str] = None,
    force: bool = False,
    caption_max: int = 200,
    reply_to: Optional[int] = None,
    exclusive: bool = False,
    user_text_hint: Optional[str] = None,
    ) -> bool:

    if not TTS_ENABLED:
        return False
    try:
        raw_du = await redis_client.get(_CB_DISABLE_UNTIL_KEY.format(chat_id=chat_id))
        if raw_du is not None:
            try:
                du = float(raw_du.decode() if isinstance(raw_du, (bytes, bytearray)) else raw_du)
                if time_module.time() < du:
                    return False
            except Exception:
                pass
    except Exception:
        pass
    if exclusive:
        caption_max = 0
    must_speak = bool(force)
    if not must_speak:

        if not will_speak(voice_in=voice_in, force=force):
            return False
        must_speak = True

    bot = get_bot()

    try:
        if await redis_client.get(f"vmsg:disabled:chat:{chat_id}"):
            logger.info("Chat %s disabled voice messages; skipping TTS", chat_id)
            return False
    except Exception:
        pass

    if not is_tts_eligible_short(reply_text):
        return False

    action_record = getattr(ChatAction, "RECORD_VOICE", ChatAction.UPLOAD_VOICE)

    async def _recording_loop() -> None:
        try:
            while True:
                try:
                    await bot.send_chat_action(chat_id, action_record)
                    await asyncio.sleep(5)
                except TelegramRetryAfter as e:
                    delay = max(1.0, float(getattr(e, "retry_after", 1)))
                    await asyncio.sleep(delay)
                except (TelegramNetworkError, asyncio.TimeoutError, TimeoutError):
                    await asyncio.sleep(2)
                except (TelegramForbiddenError, TelegramBadRequest):
                    break
        except asyncio.CancelledError:
            pass
            logger.debug("recording loop cancelled for chat_id=%s", chat_id)
        except Exception:
            logger.debug("recording loop error", exc_info=True)

    rec_task = asyncio.create_task(_recording_loop())
    try:
        trimmed_preview = trim_for_tts(reply_text or "")
        tp_len = len(trimmed_preview.strip())

        if looks_like_only_symbols(trimmed_preview):
            logger.debug("TTS skipped: emoji/symbols-only text")
            return False

        if voice_in:
            if tp_len < MIN_TTS_CHARS_VOICEIN:
                logger.debug(
                    "TTS skipped: text too short for voice_in (%s < %s)",
                    tp_len, MIN_TTS_CHARS_VOICEIN
                )
                return False
        elif tp_len < MIN_TTS_CHARS:
            logger.debug("TTS skipped: text too short (%s < %s)", tp_len, MIN_TTS_CHARS)
            return False

        tmp_path = await generate_voice_for_reply(
            reply_text=reply_text,
            user_id=user_id,
            chat_id=chat_id,
            voice_in=voice_in,
            override_voice_id=override_voice_id,
            force=must_speak,
            user_text_hint=user_text_hint,
        )
    finally:
        rec_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rec_task
    if not tmp_path:
        return False

    cap: Optional[str] = None
    if caption_max and caption_max > 0:
        _cap = strip_markup(reply_text or "").strip()
        if len(_cap) > caption_max:
            _cap = _cap[:caption_max - 1].rstrip() + "…"
        cap = _cap or None

    try:
        with contextlib.suppress(Exception):
            await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)

        try:
            await bot.send_voice(
                chat_id=chat_id,
                voice=FSInputFile(tmp_path),
                caption=cap if cap else None,
                reply_to_message_id=reply_to if reply_to else None,
                allow_sending_without_reply=True,
            )
        except TelegramForbiddenError:
            logger.info("TTS: Forbidden for chat %s — cooldown TTS", chat_id)
            try:
                await redis_client.set(
                    _CB_DISABLE_UNTIL_KEY.format(chat_id=chat_id),
                    time_module.time() + float(_CB_COOLDOWN_SEC),
                    ex=int(_CB_COOLDOWN_SEC),
                )
                await redis_client.delete(_CB_FAILCOUNT_KEY.format(chat_id=chat_id))
            except Exception:
                pass
            return False
        except TelegramBadRequest as e:
            el = str(e).lower()
            if ("voice_messages_forbidden" in el) or ("voice messages are forbidden" in el):
                with contextlib.suppress(Exception):
                    await redis_client.setex(f"vmsg:disabled:chat:{chat_id}", 30*24*3600, 1)
                logger.info("Voice forbidden in chat %s → falling back to text", chat_id)
                return False
            if "reply" in el and reply_to:
                logger.debug("Voice send failed due to reply_to, retrying without reply_to")
                await bot.send_voice(
                    chat_id=chat_id,
                    voice=FSInputFile(tmp_path),
                    caption=cap if cap else None,
                    allow_sending_without_reply=True,
                )
            else:
                raise
    except Exception:
        logger.exception("Failed to send voice message")

        try:
            fc = await redis_client.incr(_CB_FAILCOUNT_KEY.format(chat_id=chat_id))
            await redis_client.expire(_CB_FAILCOUNT_KEY.format(chat_id=chat_id), 3600)
            if int(fc) >= int(_CB_FAILS):
                await redis_client.set(
                    _CB_DISABLE_UNTIL_KEY.format(chat_id=chat_id),
                    time_module.time() + float(_CB_COOLDOWN_SEC),
                    ex=int(_CB_COOLDOWN_SEC)
                )
                await redis_client.delete(_CB_FAILCOUNT_KEY.format(chat_id=chat_id))
                logger.warning("TTS circuit breaker TRIPPED: failures>=%d, cooldown=%ds", _CB_FAILS, _CB_COOLDOWN_SEC)
        except Exception:
            pass
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return False
    finally:
        with contextlib.suppress(Exception):
            os.remove(tmp_path)

    with contextlib.suppress(Exception):
        await redis_client.delete(f"vmsg:disabled:chat:{chat_id}")

    with contextlib.suppress(Exception):
        await redis_client.delete(_CB_FAILCOUNT_KEY.format(chat_id=chat_id))

    return True