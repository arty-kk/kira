#app/services/responder/coref/resolve_coref.py
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
    while e > s and query[e-1].isspace():
        e -= 1
    while s < e and query[s] in OPEN:
        s += 1
    while e > s and query[e-1] in CLOSE:
        e -= 1
    return s, e

def _looks_like_determiner(query: str, start: int, end: int) -> bool:
    i = end
    n = len(query)
    saw_space = False
    while i < n and query[i].isspace():
        saw_space = True
        i += 1
    if not saw_space or i >= n:
        return False
    cat = unicodedata.category(query[i])
    return cat[0] in ("L", "N")

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
    # CJK
    ["我", "我们", "你", "你们", "您", "私", "僕", "俺", "あなた", "君", "나", "저", "너", "당신", "우리", "너희"]
    # Arabic
    + ["أنا", "نحن", "أنت", "أنتِ", "أنتما", "أنتم", "أنتن"]
    # Hebrew
    + ["אני", "אתה", "את", "אתם", "אתן", "אנחנו"]
    # Persian
    + ["من", "ما", "تو", "شما"]
    # Hindi (деон. формы без падежей)
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

_EXTRACT_PROMPT = """You are a multilingual coreference/deixis extractor.

INPUT:
We provide:
  (1) SNIPPET = a JSON array of recent messages, each object has fields:
      {"i": <int>, "role": "user"|"assistant", "content": "<string>"}
      Antecedent offsets MUST be measured **inside the 'content' string** of the object with the given "i".
  (2) QUERY = the latest user message (a raw string).
Your job is to identify ONLY pronouns in QUERY that should be resolved, and point to their antecedents inside SNIPPET[*].content.

RESOLVE ONLY:
- third-person pronouns (stand-alone forms) in ANY language.
- pronominal demonstratives in ANY language (used ALONE, not before a noun). Examples of such demonstratives:
  EN: this, that, these, those
  RU: это, то, тот, та, те
  (Other languages: provide the correct local form if and only if it is used pronominally/stand-alone.)

NEVER RESOLVE:
- ANY first- or second-person forms (including possessives) in ANY language.
- demonstratives used as determiners before a noun (e.g., "that book"; RU: "эта/этот/эти + NOUN").
- complementizers/relative markers (e.g., EN "that" as conjunction; RU "что" as conjunction).
- anything ambiguous or with low confidence.

OUTPUT STRICT JSON ONLY with this schema:

{
  "links": [
    {
      "pronoun_text": "<exact substring from QUERY>",
      "start": <int start index in QUERY>,
      "end": <int end index in QUERY>,
      "person": "third" | "unknown",
      "pos": "pronoun" | "demonstrative" | "determiner" | "complementizer" | "relative" | "other",
      "standalone": true | false,
      "language": "<bcp47 or iso639 guess, e.g., 'ru', 'en', 'es', 'zh', 'ja', 'tr', 'ar'>",
      "confidence": <float 0..1>,
      "antecedent_text": "<exact substring from SNIPPET[i].content>",
      "msg_index": <int 'i' of the SNIPPET object>,
      "antecedent_start": <int start index inside SNIPPET[i].content>,
      "antecedent_end": <int end index inside SNIPPET[i].content>
    }
  ]
}

CONSTRAINTS:
- Include ONLY items that are CLEAR and UNAMBIGUOUS and meet the "RESOLVE ONLY" criteria.
- "antecedent_text" MUST exactly equal SNIPPET[msg_index].content[antecedent_start:antecedent_end].
- "standalone" MUST be true for demonstratives; otherwise treat as determiner and exclude.
- If nothing to resolve, return {"links": []}.
- JSON only. No comments. No markdown fences unless using a json code block.
"""

def _validate_links(obj: Dict[str, Any], query: str, snippet_texts: List[str]) -> List[Dict[str, Any]]:
    links = obj.get("links") if isinstance(obj, dict) else None
    if not isinstance(links, list):
        return []

    n = len(query)
    out: List[Dict[str, Any]] = []
    used = []

    try:
        iter_links = sorted(
            links,
            key=lambda it: (-float(it.get("confidence", 0.0)), int(it.get("start", 10**9)))
        )
    except Exception:
        iter_links = links

    for item in iter_links:
        try:
            ptxt = str(item["pronoun_text"])
            start = int(item["start"])
            end = int(item["end"])
            person = str(item.get("person", "unknown")).strip().lower()
            pos = str(item.get("pos", "other")).strip().lower()
            standalone = bool(item.get("standalone", False))
            lang = str(item.get("language", "und")).strip().lower()
            conf = float(item.get("confidence", 0.0))
            ant = str(item["antecedent_text"])
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

        t_start, t_end = _trim_span(query, start, end)
        if not (0 <= t_start < t_end <= n):
            continue
        if (t_start, t_end) != (start, end):
            start, end = t_start, t_end
        slice_query = query[start:end]
        if slice_query != ptxt:
            ptxt = slice_query

        if not ptxt or "\n" in ptxt or len(ptxt) > 24:
            continue

        if _looks_like_first_or_second(ptxt):
            continue

        slice_ante = snip[a_s:a_e]
        if slice_ante != ant:
            ant = slice_ante

        if len(ant.strip()) == 0 or len(ant) > MAX_ANT_LENGTH:
            continue

        if ant.strip().lower() == ptxt.strip().lower():
            continue

        is_third = (person == "third" and pos in ("pronoun", "other"))
        is_demo = (pos == "demonstrative" and bool(standalone) is True)
        if not (is_third or is_demo):
            continue

        if is_demo and _looks_like_determiner(query, start, end):
            continue

        if conf < MIN_CONFIDENCE:
            continue

        if any(not (end <= s or start >= e) for s, e in used):
            continue

        used.append((start, end))
        out.append({
            "pronoun_text": ptxt,
            "start": start,
            "end": end,
            "person": person,
            "pos": pos,
            "standalone": standalone,
            "language": lang,
            "confidence": conf,
            "antecedent_text": ant,
            "msg_index": mi,
            "antecedent_start": a_s,
            "antecedent_end": a_e,
        })

    out.sort(key=lambda x: x["start"], reverse=True)
    if len(out) > MAX_LINKS:
        out = out[:MAX_LINKS]
    return out

def _looks_like_first_or_second(pronoun: str) -> bool:
    s = re.sub(r"^\W+|\W+$", "", pronoun, flags=re.UNICODE).lower().strip()
    if not s:
        return False
    en = {"i","me","my","mine","we","us","our","ours","you","your","yours"}
    if re.fullmatch(r"(я|мы|ты|вы)", s): 
        return True
    if re.fullmatch(r"(мой|моя|мо[её]|мои|твой|твоя|тво[её]|твои|наш|ваш|свой)", s):
        return True
    lat = {
        # ES
        "yo","me","mi","mio","mia","nosotros","nosotras","nos","nuestro","nuestra",
        "tú","tu","te","ti","tuyo","tuya","vos","usted","ustedes","vosotros","vosotras",
        # PT
        "eu","meu","minha","nós","nos","nosso","nossa","tu","te","voce","você","vocês","voces",
        # FR
        "je","moi","mon","ma","mes","nous","notre","nos","tu","toi","ton","ta","tes","vous","votre","vos",
        # DE
        "ich","mich","mir","wir","uns","du","dich","dir","ihr","euch","dein","euer",
        # IT
        "io","me","mio","mia","noi","nostro","nostra","tu","te","tuo","tua","voi","vostro","vostra",
        # NL
        "ik","mij","mijn","wij","we","ons","onze","jij","je","jou","jouw","u","jullie",
        # TR
        "ben","bana","beni","biz","bize","bizi","sen","sana","seni","siz","size","sizi"
    }
    cjk = {"我","我们","你","你们","您","私","僕","俺","あなた","君","貴方","나","저","너","당신","우리","너희"}
    ar = {"أنا","نحن","أنت","أنتِ","أنتما","أنتم","أنتن"}
    return s in en or s in lat or s in cjk or s in ar

async def resolve_coref(text: str, history: List[Dict[str, str]]) -> str:

    snippet = [m for m in (history or []) if m.get("role") in ("user", "assistant")][-10:]
    snippet_texts = [str(m.get("content", "")) for m in snippet]

    snippet_items = [{"i": i, "role": m.get("role","user"), "content": str(m.get("content",""))}
                     for i, m in enumerate(snippet)]
    snippet_blob = _json_for_prompt.dumps(snippet_items, ensure_ascii=False)

    prompt_user = (
        f"SNIPPET (JSON array):\n{snippet_blob}\n\n"
        f"QUERY:\n{text}\n\n"
        f"Return STRICT JSON now."
    )

    msgs = [
        _msg("system", _EXTRACT_PROMPT),
        _msg("user", prompt_user),
    ]

    raw = None
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.REASONING_MODEL,
                input=msgs,
                max_output_tokens=900,
                temperature=0,
            ),
            timeout=settings.REASONING_MODEL_TIMEOUT, 
        )
        raw = (_get_output_text(resp) or "").strip()
    except asyncio.TimeoutError:
        logger.warning("coref extract timed out after %.1fs", settings.REASONING_MODEL_TIMEOUT)
    except Exception:
        logger.exception("coref extract failed", exc_info=True)

    if not raw:
        return text

    try:
        obj = _parse_json(raw)
    except Exception:
        logger.warning("coref extract returned non-JSON; keeping original")
        return text

    links = _validate_links(obj, text, snippet_texts)
    if not links:
        return text

    rewritten = _apply_links(text, links)

    try:
        if _has_12p_disappearance(text, rewritten):
            return text
    except Exception:
        pass

    if len(rewritten) > MAX_OUTPUT_MULT * max(10, len(text)):
        return text

    return rewritten