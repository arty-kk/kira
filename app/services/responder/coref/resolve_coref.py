# app/services/responder/coref/resolve_coref.py
import logging
import asyncio
import json
import re
import json as _json_for_prompt
import unicodedata

from typing import List, Dict, Any

from app.clients.openai_client import _call_openai_with_retry, _msg, _get_output_text
from app.config import settings

logger = logging.getLogger(__name__)


MAX_LINKS = 6
MIN_CONFIDENCE = 0.80
MAX_ANT_LENGTH = 200
MAX_OUTPUT_MULT = 4

SNIPPET_MAX_MESSAGES = 8  # можно уменьшить до 8, если хотите ещё дешевле


def _parse_json(s: str) -> Dict[str, Any]:
    raw = s.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE).strip()
    return json.loads(raw)

def _trim_span(query: str, start: int, end: int) -> tuple[int, int]:
    OPEN = set('([{"«“‘')
    CLOSE = set('.,!?…:;)]}>"»”’')
    n = len(query)
    s, e = max(0, start), min(end, n)

    while s < e and query[s].isspace():
        s += 1
    while e > s and query[e - 1].isspace():
        e -= 1
    while s < e and query[s] in OPEN:
        s += 1
    while e > s and query[e - 1] in CLOSE:
        e -= 1
    return s, e

def _next_word(query: str, i: int) -> str:
    n = len(query)
    while i < n and query[i].isspace():
        i += 1
    j = i
    while j < n and (query[j].isalpha() or query[j] in ("'", "’")):
        j += 1
    return query[i:j].lower()

def _looks_like_determiner(query: str, start: int, end: int) -> bool:
    """
    Более аккуратная версия:
    пытаемся отличать pronominal demonstrative ("this was ...", "это было ...")
    от determiner ("this book", "эта книга").
    """
    i = end
    n = len(query)

    saw_space = False
    while i < n and query[i].isspace():
        saw_space = True
        i += 1
    if not saw_space or i >= n:
        return False

    nxt = _next_word(query, i)

    # Частые связки/вспомогательные глаголы → это НЕ determiner перед существительным
    en_non_noun = {
        "is", "are", "was", "were", "be", "been", "being",
        "do", "does", "did",
        "have", "has", "had",
        "can", "could", "may", "might", "must",
        "will", "would", "shall", "should",
    }
    ru_non_noun = {
        "был", "была", "были", "было",
        "будет", "будут",
        "есть", "значит",
    }
    if nxt in en_non_noun or nxt in ru_non_noun:
        return False

    # Иначе — считаем determiner-подобным (консервативно)
    cat = unicodedata.category(query[i])
    return cat[0] in ("L", "N")

def _looks_like_existential_there(query: str, start: int, end: int) -> bool:
    tok = query[start:end].strip().lower()
    if tok in ("there's", "there’s"):
        return True
    if tok != "there":
        return False

    i = end
    n = len(query)
    while i < n and query[i].isspace():
        i += 1
    j = i
    while j < n and (query[j].isalpha() or query[j] in ("'", "’")):
        j += 1
    nxt = query[i:j].lower()
    return nxt in ("is", "are", "was", "were", "'s", "’s")

def _deictic_is_anchored(query: str, start: int, end: int) -> bool:
    """
    Улучшено: пропускаем пунктуацию между деиктиком и предлогом.
    Примеры, которые должны резаться:
      EN: "here, in Amsterdam", "there — at the office"
      RU: "там, в офисе", "здесь — на улице"
    """
    i = end
    n = len(query)

    while i < n:
        ch = query[i]
        if ch.isspace():
            i += 1
            continue
        # пропускаем пунктуацию/кавычки/тире и т.п.
        if unicodedata.category(ch).startswith("P") or ch in ("—", "–", "-", "…"):
            i += 1
            continue
        break

    if i >= n:
        return False

    nxt = _next_word(query, i)

    en_preps = {
        "in", "at", "on", "near", "from", "to", "into", "onto", "over", "under", "between",
        "around", "inside", "outside", "within", "by", "of"
    }
    ru_preps = {
        "в", "во", "на", "у", "около", "возле", "рядом", "с", "со", "из", "от", "до", "к", "ко",
        "по", "под", "над", "между", "перед", "за"
    }
    return nxt in en_preps or nxt in ru_preps


_RX_12P = [
    re.compile(r"\b(I|me|my|mine|we|us|our|ours|you|your|yours)\b", re.IGNORECASE),
    re.compile(r"\b(я|меня|мне|мной|мы|нас|нам|нами|ты|тебя|тебе|тобой|вы|вас|вам|вами|"
               r"мой|моя|моё|мои|моего|моему|моим|моём|моей|мою|моими|моих|"
               r"твой|твоя|твоё|твои|твоего|твоему|твоим|твоём|твоей|твою|твоими|твоих|"
               r"наш|наша|наше|наши|ваш|ваша|ваше|ваши|свой|своя|своё|свои)\b", re.IGNORECASE),
    re.compile(r"\b(yo|me|mi|mio|mia|mios|mias|nosotros|nosotras|nos|nuestro|nuestra|nuestros|nuestras|"
               r"tú|tu|te|ti|tuyo|tuya|tuyos|tuyas|vos|usted|ustedes|vosotros|vosotras)\b", re.IGNORECASE),
    re.compile(r"\b(eu|meu|minha|meus|minhas|nós|nos|nosso|nossa|nossos|nossas|"
               r"tu|te|você|vocês|voce|voces)\b", re.IGNORECASE),
    re.compile(r"\b(je|moi|mon|ma|mes|nous|notre|nos|tu|toi|ton|ta|tes|vous|votre|vos)\b", re.IGNORECASE),
    re.compile(r"\b(ich|mich|mir|wir|uns|du|dich|dir|ihr|euch|dein|euer)\b", re.IGNORECASE),
    re.compile(r"\b(io|me|mio|mia|miei|mie|noi|nostro|nostra|nostri|nostre|tu|te|tuo|tua|tuoi|tue|voi|vostro|vostra|vostri|vostre)\b", re.IGNORECASE),
    re.compile(r"\b(ik|mij|mijn|wij|we|ons|onze|jij|je|jou|jouw|u|jullie)\b", re.IGNORECASE),
    re.compile(r"\b(ben|bana|beni|biz|bize|bizi|sen|sana|seni|siz|size|sizi)\b", re.IGNORECASE),
    re.compile(r"\b(ja|mnie|mi|mój|moja|moje|my|nas|nam|nasz|nasza|nasze|ty|ciebie|ci|twój|twoja|twoje|wy|was|wam|wasz|wasza|wasze)\b", re.IGNORECASE),
]
_SUBSTR_12P = (
    ["我", "我们", "你", "你们", "您", "私", "僕", "俺", "あなた", "君", "나", "저", "너", "당신", "우리", "너희"]
    + ["أنا", "نحن", "أنت", "أنتِ", "أنتما", "أنتم", "أنتن"]
    + ["אני", "אתה", "את", "אתם", "אתן", "אנחנו"]
    + ["من", "ما", "تو", "شما"]
    + ["मैं", "हम", "तुम", "आप", "तू"]
)

def _has_12p_disappearance(original: str, rewritten: str) -> bool:
    for rx in _RX_12P:
        for m in rx.finditer(original):
            term = m.group(0)
            if not re.search(rf"\b{re.escape(term)}\b", rewritten, flags=re.IGNORECASE):
                return True
    for term in _SUBSTR_12P:
        if term in original and term not in rewritten:
            return True
    return False

def _apply_links(query: str, links: List[Dict[str, Any]]) -> str:
    out = query
    for it in sorted(links, key=lambda x: x["start"], reverse=True):
        s, e = it["start"], it["end"]
        ant = it["antecedent_text"]
        out = out[:s] + ant + out[e:]
    return out

def _looks_like_first_or_second(pronoun: str) -> bool:
    s = re.sub(r"^\W+|\W+$", "", pronoun, flags=re.UNICODE).lower().strip()
    if not s:
        return False
    en = {"i", "me", "my", "mine", "we", "us", "our", "ours", "you", "your", "yours"}
    if re.fullmatch(r"(я|мы|ты|вы)", s):
        return True
    if re.fullmatch(r"(мой|моя|мо[её]|мои|твой|твоя|тво[её]|твои|наш|ваш|свой)", s):
        return True
    lat = {
        "yo", "me", "mi", "mio", "mia", "nosotros", "nosotras", "nos", "nuestro", "nuestra",
        "tú", "tu", "te", "ti", "tuyo", "tuya", "vos", "usted", "ustedes", "vosotros", "vosotras",
        "eu", "meu", "minha", "nós", "nos", "nosso", "nossa", "tu", "te", "voce", "você", "vocês", "voces",
        "je", "moi", "mon", "ma", "mes", "nous", "notre", "nos", "tu", "toi", "ton", "ta", "tes", "vous", "votre", "vos",
        "ich", "mich", "mir", "wir", "uns", "du", "dich", "dir", "ihr", "euch", "dein", "euer",
        "io", "me", "mio", "mia", "noi", "nostro", "nostra", "tu", "te", "tuo", "tua", "voi", "vostro", "vostra",
        "ik", "mij", "mijn", "wij", "we", "ons", "onze", "jij", "je", "jou", "jouw", "u", "jullie",
        "ben", "bana", "beni", "biz", "bize", "bizi", "sen", "sana", "seni", "siz", "size", "sizi"
    }
    cjk = {"我", "我们", "你", "你们", "您", "私", "僕", "俺", "あなた", "君", "貴方", "나", "저", "너", "당신", "우리", "너희"}
    ar = {"أنا", "نحن", "أنت", "أنتِ", "أنتما", "أنتم", "أنتن"}
    return s in en or s in lat or s in cjk or s in ar


_EXTRACT_PROMPT = """You extract coreference/deixis links between the latest user QUERY and prior chat SNIPPET.

SNIPPET is a JSON array of objects: {"i":int, "r":"u"|"a", "c":string}
Antecedent offsets MUST be measured inside SNIPPET[msg_index].c.

Return links ONLY when HIGH confidence and UNAMBIGUOUS:
- third-person pronouns (stand-alone forms),
- pronominal demonstratives (stand-alone; not determiner),
- discourse deictic adverbs (here/there/now/then; здесь/там/сейчас/тогда etc) ONLY if SNIPPET contains a concrete antecedent span.

Exclude:
- any 1st/2nd person forms,
- demonstratives used as determiners before a noun,
- complementizer/conjunction "that"/"что",
- EN existential "there is/are/was/were" / "there's",
- anchored deictics like "here in X" or "там в Y".

Output must match the given JSON schema. JSON only, no extra text.
"""

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                    "pos": {"type": "string", "enum": ["pronoun", "demonstrative", "deictic"]},
                    "standalone": {"type": "boolean"},
                    "confidence": {"type": "number"},
                    "msg_index": {"type": "integer"},
                    "antecedent_start": {"type": "integer"},
                    "antecedent_end": {"type": "integer"},
                },
                "required": ["start", "end", "pos", "standalone", "confidence", "msg_index", "antecedent_start", "antecedent_end"],
                "additionalProperties": False
            }
        }
    },
    "required": ["links"],
    "additionalProperties": False
}

def _validate_links(obj: Dict[str, Any], query: str, snippet_texts: List[str]) -> List[Dict[str, Any]]:
    links = obj.get("links") if isinstance(obj, dict) else None
    if not isinstance(links, list):
        return []

    n = len(query)
    out: List[Dict[str, Any]] = []
    used = []

    # сортируем по confidence desc, потом start asc
    try:
        iter_links = sorted(
            links,
            key=lambda it: (-float(it.get("confidence", 0.0)), int(it.get("start", 10**9)))
        )
    except Exception:
        iter_links = links

    for item in iter_links:
        try:
            start = int(item["start"])
            end = int(item["end"])
            pos = str(item.get("pos", "other")).strip().lower()
            standalone = bool(item.get("standalone", False))
            conf = float(item.get("confidence", 0.0))
            mi = int(item["msg_index"])
            a_s = int(item["antecedent_start"])
            a_e = int(item["antecedent_end"])
        except Exception:
            continue

        if not (0 <= start < end <= n):
            continue
        if not (0 <= mi < len(snippet_texts)):
            continue

        snip = snippet_texts[mi]
        if not (0 <= a_s < a_e <= len(snip)):
            continue

        # нормализуем границы по пунктуации/скобкам
        t_start, t_end = _trim_span(query, start, end)
        if not (0 <= t_start < t_end <= n):
            continue
        start, end = t_start, t_end

        ptxt = query[start:end]
        if not ptxt or "\n" in ptxt or len(ptxt) > 24:
            continue
        if _looks_like_first_or_second(ptxt):
            continue

        ant = snip[a_s:a_e]
        if len(ant.strip()) == 0 or len(ant) > MAX_ANT_LENGTH:
            continue
        if ant.strip().lower() == ptxt.strip().lower():
            continue

        if pos == "demonstrative":
            if not standalone:
                continue
            if _looks_like_determiner(query, start, end):
                continue

        elif pos == "deictic":
            if _looks_like_existential_there(query, start, end):
                continue
            if _deictic_is_anchored(query, start, end):
                continue

        elif pos == "pronoun":
            # доп. фильтров нет; первые/вторые уже отсеяны
            pass
        else:
            continue

        if conf < MIN_CONFIDENCE:
            continue

        # не даём перекрывающиеся замены
        if any(not (end <= s or start >= e) for s, e in used):
            continue

        used.append((start, end))
        out.append({
            "start": start,
            "end": end,
            "pos": pos,
            "standalone": standalone,
            "confidence": conf,
            "msg_index": mi,
            "antecedent_start": a_s,
            "antecedent_end": a_e,
            "antecedent_text": ant,
        })

    out.sort(key=lambda x: x["start"], reverse=True)
    if len(out) > MAX_LINKS:
        out = out[:MAX_LINKS]
    return out


async def resolve_coref(text: str, history: List[Dict[str, str]]) -> str:
    if text is None:
        return ""

    query = str(text)

    snippet = [m for m in (history or []) if m.get("role") in ("user", "assistant")][-SNIPPET_MAX_MESSAGES:]
    if not snippet:
        return query

    snippet_texts = [str(m.get("content", "")) for m in snippet]
    if not any(s.strip() for s in snippet_texts):
        return query

    # компактный формат для модели → меньше токенов
    snippet_items = []
    for i, m in enumerate(snippet):
        role = m.get("role", "user")
        r = "u" if role == "user" else "a"
        c = str(m.get("content", ""))
        snippet_items.append({"i": i, "r": r, "c": c})

    snippet_blob = _json_for_prompt.dumps(snippet_items, ensure_ascii=False)

    prompt_user = (
        f"SNIPPET:\n{snippet_blob}\n\n"
        f"QUERY:\n{query}\n"
    )

    msgs = [
        _msg("system", _EXTRACT_PROMPT),
        _msg("user", prompt_user),
    ]

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.REASONING_MODEL,
                input=msgs,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "coref_links",
                        "strict": True,
                        "schema": _EXTRACT_SCHEMA,
                    }
                },
                max_output_tokens=512,  # теперь достаточно, т.к. формат компактный
                temperature=0,
            ),
            timeout=settings.REASONING_MODEL_TIMEOUT,
        )
        raw = (_get_output_text(resp) or "").strip()
    except asyncio.TimeoutError:
        logger.warning("coref extract timed out after %.1fs", settings.REASONING_MODEL_TIMEOUT)
        return query
    except Exception:
        logger.exception("coref extract failed", exc_info=True)
        return query

    if not raw:
        return query

    # json_schema должен гарантировать валидный JSON, но оставим fallback на всякий
    try:
        obj = json.loads(raw)
    except Exception:
        try:
            obj = _parse_json(raw)
        except Exception:
            logger.warning("coref extract returned non-JSON; keeping original")
            return query

    links = _validate_links(obj, query, snippet_texts)
    if not links:
        return query

    rewritten = _apply_links(query, links)

    try:
        if _has_12p_disappearance(query, rewritten):
            return query
    except Exception:
        pass

    if len(rewritten) > MAX_OUTPUT_MULT * max(10, len(query)):
        return query

    return rewritten
