#app/services/responder/context_select.py
from __future__ import annotations

import asyncio
import json
import logging
import re
import time as time_module

from typing import Dict, List, Optional, Iterable, Tuple
from collections import Counter

from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from app.config import settings
from app.prompts_base import (
    CONTEXT_EXPAND_QUERY_PROMPT_TEMPLATE,
    CONTEXT_RERANK_PROMPT_TEMPLATE,
    CONTEXT_TOPIC_SUMMARY_PROMPT_TEMPLATE,
    context_select_snippets_default_prompt,
    context_select_snippets_mtm_prompt,
)
from app.core.memory import approx_tokens

logger = logging.getLogger(__name__)

_NANO_TIMEOUT_LTM = settings.BASE_MODEL_TIMEOUT
_NANO_TIMEOUT_MTM = settings.BASE_MODEL_TIMEOUT
_JSON_ARRAY_RE = re.compile(r"\[\s*(?:\d+\s*(?:,\s*\d+\s*)*)?\]")
_ROLE_RE = re.compile(r"^\s*(user|assistant)\s*:\s*(.*)$", re.I)

def _safe_approx_total_tokens(texts: Iterable[str]) -> int:

    total = 0
    approx_cpt = settings.APPROX_CHARS_PER_TOKEN
    for s in texts:
        if not s:
            continue
        try:
            total += approx_tokens(s)
        except Exception:
            total += max(1, int(len(s) / approx_cpt))
    return total


def _dynamic_parallel_by_tokens(total_tokens: int) -> int:

    try:
        step = settings.MEMORY_PARALLEL_TOKENS_PER_REQUEST
    except Exception:
        step = 8000

    try:
        hard_cap = settings.MEMORY_PARALLEL_MAX_REQUESTS
    except Exception:
        hard_cap = 10

    try:
        api_cap = settings.OPENAI_MAX_CONCURRENT_REQUESTS
    except Exception:
        api_cap = 100

    max_parallel = max(1, min(hard_cap, api_cap))

    if total_tokens <= 0:
        return 1

    n = (int(total_tokens) - 1) // max(1, step) + 1
    if n < 1:
        n = 1
    if n > max_parallel:
        n = max_parallel

    return int(n)

def _log_parallel_plan(label: str, texts: Iterable[str], parallel: int) -> None:

    try:
        total = _safe_approx_total_tokens(texts)
    except Exception:
        total = -1

    try:
        dyn = _dynamic_parallel_by_tokens(max(0, total))
    except Exception:
        dyn = -1

    logger.info(
        "ParallelPlan[%s]: total_tokens≈%d dyn_parallel=%d effective_parallel=%d",
        label,
        total,
        dyn,
        parallel,
    )


def _parse_unix_ts_prefix(s: str) -> tuple[int, str]:
    if not s.startswith("["):
        return (0, s)
    try:
        end = s.find("]")
        ts = int(s[1:end])
        return (ts, s[end+1:].strip())
    except Exception:
        return (0, s)

def _fmt_date_utc(ts: int) -> str:

    try:
        if ts <= 0:
            return ""
        t = time_module.gmtime(ts)
        return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
    except Exception:
        return ""

def _extract_int_array(text: str) -> list[int]:

    if not text:
        return []
    m = _JSON_ARRAY_RE.search(text)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
        out: list[int] = []
        if not isinstance(arr, list):
            return []
        for x in arr:
            if isinstance(x, int):
                if x >= 0:
                    out.append(int(x))
                continue
            if isinstance(x, str):
                xs = x.strip()
                if xs.isdigit():
                    v = int(xs)
                    if v >= 0:
                        out.append(v)
        return out
    except Exception:
        return []

def _soft_trim(s: str, char_limit: int) -> str:
    if char_limit <= 0 or len(s) <= char_limit:
        return s

    cut = s[:char_limit]

    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= int(char_limit * 0.6):
        return cut[:end+1]

    sp = cut.rfind(" ")
    return cut[:sp] if sp > 0 else cut

def _pairize_mtm(lines: List[str]) -> List[str]:

    out: List[str] = []
    cur_user: Optional[Tuple[int, str]] = None
    for ln in lines:
        ts, rest = _parse_unix_ts_prefix(ln)
        m = _ROLE_RE.match(rest)
        role = m.group(1).lower() if m else ""
        text = (m.group(2) if m else rest).strip()
        if not role:
            if cur_user:
                out.append(f"[{cur_user[0]}] user: {cur_user[1]}")
                cur_user = None
            out.append(ln.strip())
            continue

        if role == "user":
            if cur_user:
                out.append(f"[{cur_user[0]}] user: {cur_user[1]}")
            cur_user = (ts, text)
        elif role == "assistant":
            if cur_user:
                u_ts, u_txt = cur_user
                pair = f"[{u_ts}] user: {u_txt}\n[{ts}] assistant: {text}"
                out.append(pair)
                cur_user = None
            else:
                out.append(f"[{ts}] assistant: {text}")

    if cur_user:
        out.append(f"[{cur_user[0]}] user: {cur_user[1]}")
    return out

def _tail_biased_take(items: List[str], cap: int, recent_take: int) -> List[str]:

    if cap <= 0 or not items:
        return []
    if len(items) <= cap:
        return list(items)
    rt = max(0, min(recent_take, cap))
    recent = items[-rt:] if rt > 0 else []
    rest = items[:-rt] if rt > 0 else items
    need = cap - len(recent)
    if need <= 0:
        return items[-cap:]
    if not rest:
        return recent
    step = max(1, len(rest) // need)
    head_pick = rest[::step][:need]
    return head_pick + recent

async def rerank_with_llm(query: str, items: List[str], topk: int = 20, batch: int = 40) -> List[str]:
    if not items:
        return []

    topk = max(1, int(topk))

    per_item_limit = settings.RERANK_ITEM_CHAR_LIMIT

    async def _ask(q: str, chunk: List[tuple[int, str]], k: int) -> List[int]:
        lines = "\n".join(
            f"[{i}] <<<\n{_soft_trim(t, per_item_limit)}\n>>>"
            for i, t in chunk
        )
        prompt = CONTEXT_RERANK_PROMPT_TEMPLATE.format(k=k, query=q, lines=lines)

        try:
            _timeout = settings.BASE_MODEL_TIMEOUT
            _t0 = time_module.perf_counter()
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="responses.create",
                    model=settings.BASE_MODEL,
                    model_role="base",
                    input=prompt,
                    max_output_tokens=256,
                    temperature=0,
                ),
                timeout=_timeout,
            )
            _dt = time_module.perf_counter() - _t0
            logger.info(
                "OpenAI call duration: endpoint=responses.create purpose=rerank "
                "model=%s items=%d took=%.3fs timeout=%.2fs",
                settings.BASE_MODEL, len(chunk), _dt, _timeout,
            )
        except asyncio.TimeoutError:
            _dt = time_module.perf_counter() - _t0
            logger.warning(
                "OpenAI timeout: endpoint=responses.create purpose=rerank "
                "after=%.3fs timeout=%.2fs items=%d",
                _dt, _timeout, len(chunk),
            )
            return []
        except Exception as exc:
            logger.warning(
                "OpenAI call failed: endpoint=responses.create purpose=rerank "
                "items=%d err=%r",
                len(chunk), exc,
            )
            return []

        txt = (_get_output_text(resp) or "").strip()
        return _extract_int_array(txt)

    pairs = list(enumerate(items))

    def _fallback(items_: List[str], k_: int) -> List[str]:
        recent_take = max(1, int(k_ // 2))
        return _tail_biased_take(items_, cap=k_, recent_take=recent_take)

    if len(pairs) <= batch:
        idxs = await _ask(query, pairs, min(topk, len(pairs)))
        picked = [items[i] for i in idxs if 0 <= i < len(items)]
        return picked if picked else _fallback(items, topk)

    total_tokens = _safe_approx_total_tokens(items)
    dyn_parallel = _dynamic_parallel_by_tokens(total_tokens)
    desired_chunks = max(1, min(dyn_parallel, len(pairs)))

    ideal_chunk_size = max(
        max(1, int(batch)),
        (len(pairs) + desired_chunks - 1) // desired_chunks,
    )

    chunks: List[List[tuple[int, str]]] = [
        pairs[s:s + ideal_chunk_size]
        for s in range(0, len(pairs), ideal_chunk_size)
    ]

    try:
        api_cap = settings.OPENAI_MAX_CONCURRENT_REQUESTS
    except Exception:
        api_cap = 100

    parallel = max(1, min(dyn_parallel, api_cap, len(chunks)))

    _log_parallel_plan("Rerank[LLM]", items, parallel)

    logger.info(
        "Rerank[LLM]: items=%d total_tokens≈%d chunks=%d parallel=%d topk=%d batch=%d",
        len(items), total_tokens, len(chunks), parallel, topk, batch,
    )

    sem = asyncio.Semaphore(parallel)

    async def _run_chunk(chunk: List[tuple[int, str]]) -> List[int]:
        async with sem:
            picked = await _ask(query, chunk, min(topk, len(chunk)))
            allowed = {i for i, _ in chunk}
            return [i for i in picked if i in allowed]

    results = await asyncio.gather(
        *(_run_chunk(ch) for ch in chunks),
        return_exceptions=True,
    )

    batch_tops: List[int] = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning("Rerank[LLM] chunk failed: %r", r)
            continue
        batch_tops.extend(r)

    if not batch_tops:
        return _fallback(items, topk)

    freq = Counter(batch_tops)
    uniq_sorted = [i for i, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))]

    final_chunk = [(i, items[i]) for i in uniq_sorted]
    final_idx = await _ask(query, final_chunk, min(topk, len(final_chunk))) or uniq_sorted[:topk]

    allowed_final = set(uniq_sorted)
    final_idx = [
        i for i in final_idx
        if i in allowed_final and 0 <= i < len(items)
    ]

    return [items[i] for i in final_idx[:topk]]

async def _expand_queries_for_ltm(query: str) -> List[str]:

    q = (query or "").strip()
    if not q:
        return []
    prompt = CONTEXT_EXPAND_QUERY_PROMPT_TEMPLATE.format(query=q)
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.BASE_MODEL,
                model_role="base",
                input=prompt,
                max_output_tokens=160,
                temperature=0,
            ),
            timeout=settings.BASE_MODEL_TIMEOUT,
        )
        txt = (_get_output_text(resp) or "").strip()
        arr = []
        try:
            arr = json.loads(txt)
            if not isinstance(arr, list):
                arr = []
        except Exception:
            arr = []
        outs = [q] + [str(x).strip() for x in arr if str(x).strip()]
        seen = set()
        uniq = []
        for s in outs:
            k = s.casefold()
            if k in seen:
                continue
            seen.add(k)
            uniq.append(s)
        return uniq[:4]
    except Exception:
        return [q]

async def select_snippets_via_nano(
    source: str,
    query: str,
    candidates: List[str],
    max_tokens: int,
    model: Optional[str] = None,
    model_role: Optional[str] = None,
    mtm_stage: Optional[str] = None,
) -> str:
    
    if not query or not candidates:
        return ""
    
    src = (source or "").upper()
    C_MAX = 40

    if src == "MTM":
        short = list(candidates)
    else:
        short = candidates[:C_MAX]

    if src == "MTM":
        prompt = context_select_snippets_mtm_prompt(query, short, max_tokens)
    else:
        prompt = context_select_snippets_default_prompt(source, query, short, max_tokens)
    try:
        _t0 = time_module.perf_counter()
        timeout_sel = _NANO_TIMEOUT_MTM if src == "MTM" else _NANO_TIMEOUT_LTM
        chosen_model = model or settings.BASE_MODEL
        inferred_role = "base" if model is None else "regular"
        chosen_model_role = (model_role or inferred_role).strip().lower()
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=chosen_model,
                model_role=chosen_model_role,
                input=prompt,
                max_output_tokens=max_tokens,
                temperature=0,
            ),
            timeout=timeout_sel
        )
        out = (_get_output_text(resp) or "").strip()
    except asyncio.CancelledError:
        try:
            logger.debug("select_snippets_via_nano: cancelled (outer timeout) — yielding empty")
        except Exception:
            pass
        out = ""
    except Exception as e:
        try:
            _dt = time_module.perf_counter() - _t0
            logger.warning(
                "OpenAI call failed: endpoint=responses.create purpose=select_snippets source=%s candidates=%d after=%.3fs err=%r",
                source, min(len(candidates), C_MAX), _dt, e
            )
        except Exception:
            pass
        out = ""
    return out

async def compose_mtm_snippet(
    query: str,
    mtm_lines: List[str],
    max_tokens: int,
) -> str:

    if not query or not mtm_lines or max_tokens <= 0:
        return ""

    mtm_lines = _pairize_mtm(mtm_lines)
    
    try:
        topn = int(getattr(settings, "MTM_STAGE2_TOPN", 40))
    except Exception:
        topn = 40

    cands = await preselect_mtm_candidates(query, mtm_lines, topn=topn)
    if not cands:
        return ""

    date_re = re.compile(r"^\[(\d{4})-(\d{2})-(\d{2})\]")

    def _date_rank(s: str) -> int:
        m = date_re.match(s.strip())
        if not m:
            return 99_999_999
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return y * 10_000 + mo * 100 + d
        except Exception:
            return 99_999_999

    ordered = sorted(cands, key=_date_rank)

    acc = 0
    picked: List[str] = []
    for s in ordered:
        t = approx_tokens(s)
        if acc + t > max_tokens * 2:
            break
        picked.append(s)
        acc += t

    if not picked:
        picked = ordered[:1]

    notes = await select_snippets_via_nano(
        source="MTM",
        query=query,
        candidates=picked,
        max_tokens=max_tokens,
        mtm_stage="compose",
    )

    return notes or "\n".join(picked[:1])


def _tail_by_tokens(lines: List[str], max_tokens: int) -> List[str]:
    if max_tokens <= 0 or not lines:
        return lines
    acc = 0
    out_rev = []
    for ln in reversed(lines):
        t = approx_tokens(ln)
        acc += t
        out_rev.append(ln)
        if acc >= max_tokens:
            break
    return list(reversed(out_rev))

def _uniform_sample(lines: List[str], max_n: int) -> List[str]:
    n = len(lines)
    if n <= max_n or max_n <= 0:
        return lines
    step = n / float(max_n)
    seen = set()
    out: List[str] = []
    offset = 0
    for k in range(max_n):
        idx = int((k * step + offset) % n)
        if idx >= n:
            idx = n - 1
        if idx not in seen:
            out.append(lines[idx])
            seen.add(idx)
    return out

async def preselect_mtm_candidates(query: str, mtm_lines: List[str], topn: int = 40) -> List[str]:

    if not query or not mtm_lines:
        return []

    base = [s for s in mtm_lines if approx_tokens(s) >= 3]

    if not base:
        return []

    pairs = [_parse_unix_ts_prefix(s) for s in base]

    annotated: List[str] = []
    for ts, txt in pairs:
        date_tag = _fmt_date_utc(ts)
        annotated.append(f"[{date_tag}] {txt}" if date_tag else txt)

    if len(annotated) <= topn:
        return annotated

    llm_query = (
        "User query (it may be about the current topic OR about past events / user biography):\n"
        f"{query}\n\n"
        "Task: from the MTM fragments below, choose those that are most USEFUL to answer this query.\n"
        "- Treat \"useful\" broadly: include fragments that contain the answer or important partial clues,\n"
        "  even if they belong to a different conversation thread or use different wording.\n"
        "- Examples of such queries: \"когда мы познакомились\", \"кто мои родители\",\n"
        "  \"что я говорил про свою работу\", \"when did we first talk\", \"what is my job\".\n"
        "- For time/meta questions like \"когда мы познакомились\" / \"when did we first talk\",\n"
        "  prefer fragments from the EARLIEST part of the history where the user and assistant talk directly.\n"
        "- For questions about the user’s biography, family, identity, or long-term plans, prefer fragments\n"
        "  where the user states durable facts (parents, job, city, relationships, major projects) explicitly.\n"
        "- If relevance is similar, prefer more specific fragments or those that clearly mention the entities\n"
        "  implied by the query.\n"
        "- Ignore pure greetings/small talk that add no facts or commitments."
    )

    batch = settings.MTM_LLM_BATCH
    picked = await rerank_with_llm(llm_query, annotated, topk=topn, batch=batch)
    return picked[:topn]


async def summarize_mtm_topic(history_msgs: List[Dict], last_pairs: int = 2) -> str:

    try:
        seq = [m for m in history_msgs if (m.get("role") in ("user", "assistant"))]
        tail = seq[-(last_pairs * 2):] if seq else []
        if not tail:
            return ""
        lines = []
        for m in tail:
            r = m.get("role")
            c = (m.get("content") or "").strip()
            if c:
                lines.append(f"[{r}] {c}")
        if not lines:
            return ""

        prompt = CONTEXT_TOPIC_SUMMARY_PROMPT_TEMPLATE.format(lines="\n".join(lines))
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.BASE_MODEL,
                model_role="base",
                input=prompt,
                max_output_tokens=100,
                temperature=0,
            ),
            timeout=settings.BASE_MODEL_TIMEOUT,
        )
        topic = (_get_output_text(resp) or "").strip()
        topic = re.sub(r'^[\s\-\·]+|[.!?…\s]+$', '', topic)
        return topic
    except Exception:
        return ""

async def select_ltm_snippet(query: str, fragments: List[str], max_tokens: int) -> str:
    if not query or not fragments:
        return ""

    try:
        per_item_limit = settings.LTM_LLM_ITEM_CHAR_LIMIT
    except Exception:
        per_item_limit = 600
    seen: set[str] = set()
    prepped: List[str] = []
    for f in fragments:
        s = (f or "").strip()
        if not s:
            continue
        k = re.sub(r"^\s*\[\d{4}-\d{2}-\d{2}\]\s*", "", s, flags=re.I).casefold()
        if k in seen:
            continue
        seen.add(k)
        prepped.append(_soft_trim(s, per_item_limit))
    
    if not prepped:
        return ""

    try:
        pool_max = settings.LTM_LLM_POOL_MAX
    except Exception:
        pool_max = 200

    if len(prepped) > pool_max:
        recent_take = max(4, int(pool_max * 0.35))
        recent = prepped[-recent_take:]
        rest = _uniform_sample(prepped[:-recent_take], pool_max - recent_take)
        pool = recent + rest
    else:
        pool = prepped

    try:
        topn = settings.LTM_LLM_TOPN
    except Exception:
        topn = 20

    if len(pool) <= topn:
        picked = pool
    else:
        qexp = await _expand_queries_for_ltm(query) or [query]
        alt_block = ("\n- " + "\n- ".join(qexp[1:])) if len(qexp) > 1 else ""

        llm_query = (
            "User query (it may be about the current topic OR about past events / user biography):\n"
            f"{query}\n\n"
            "Alternative phrasings (for coverage):" + alt_block + "\n\n"
            "Task: choose long-term memory fragments that are MOST USEFUL to answer this query or personalize the reply.\n"
            "- Treat \"useful\" broadly: include fragments that contain the requested fact, preference, constraint,\n"
            "  or commitment, even if they belong to another topic or are much older.\n"
            "- For questions about the user's biography, family, name, job, location, or other identity facts,\n"
            "  prefer fragments where the user states these explicitly (e.g. parents' names, job title, city).\n"
            "- For questions about \"when\" something first happened between the user and the assistant, LTM may contain\n"
            "  only summaries; still prefer fragments that mention dates, durations, or \"first/earliest\" events.\n"
            "- Prefer durable facts, preferences, constraints, and commitments over ephemeral chit-chat.\n"
            "- Ignore pure greetings/small talk that carry no facts or commitments."
        )

        try:
            batch = settings.LTM_LLM_BATCH
        except Exception:
            batch = 30

        picked = await rerank_with_llm(llm_query, pool, topk=topn, batch=batch)

    if not picked:
        logger.info("Retrieval[LTM LLM-only]: no candidates returned; yielding empty snippet")
        return ""

    acc = 0
    out: List[str] = []
    for s in picked:
        if not s:
            continue
        t = approx_tokens(s)
        if acc + t > max_tokens:
            break
        out.append(s)
        acc += t

    if not out:
        out = picked[:1]

    return "\n\n".join(out)
