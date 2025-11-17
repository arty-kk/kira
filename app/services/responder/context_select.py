#app/services/responder/context_select.py
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time as time_module

from typing import Dict, List, Optional, Iterable, Tuple
from collections import Counter

from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from app.config import settings
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
        return [int(x) for x in arr if isinstance(x, int)]
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
        prompt = (
            "You are a reranker.\n"
            f"Task: for the query below, choose the top-{k} most relevant items from the list.\n"
            "Each item starts with a GLOBAL id in square brackets, e.g. [12]. That id is the only id you must use.\n"
            "Output: a JSON array of these ids in order of DESCENDING relevance, e.g. [3, 5, 1].\n"
            "Ignore any other brackets that appear between <<< and >>>; they are part of the content.\n"
            "Return ONLY the JSON array, with no explanation.\n"
            f"Query:\n{q}\n"
            "Items:\n" + lines
            )

        try:
            _timeout = settings.BASE_MODEL_TIMEOUT
            _t0 = time_module.perf_counter()
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="responses.create",
                    model=settings.BASE_MODEL,
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

    if len(pairs) <= batch:
        idxs = await _ask(query, pairs, min(topk, len(pairs)))
        return [items[i] for i in idxs if 0 <= i < len(items)]

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
        return items[:topk]

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
    prompt = (
        "Rewrite this query into 3 short, diverse paraphrases focusing on different aspects "
        "(intent, key entities, user-related details such as preferences or constraints).\n"
        "Return ONLY a JSON array of strings.\n"
        f"Query: {q}"
    )
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.BASE_MODEL,
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
        seen = set(); uniq = []
        for s in outs:
            k = s.casefold()
            if k in seen: 
                continue
            seen.add(k); uniq.append(s)
        return uniq[:4]
    except Exception:
        return [q]

async def select_snippets_via_nano(
    source: str,
    query: str,
    candidates: List[str],
    max_tokens: int,
    model: Optional[str] = None,
    mtm_stage: Optional[str] = None,
) -> str:
    
    if not query or not candidates:
        return ""
    
    src = (source or "").upper()
    C_MAX = 40

    if src == "MTM":
        try:
            cmax_conf = int(getattr(settings, "MTM_S1_CAND_MAX", C_MAX))
        except Exception:
            cmax_conf = C_MAX
        try:
            recent_take = int(getattr(settings, "MTM_S1_RECENT_TAKE", max(1, cmax_conf // 2)))
        except Exception:
            recent_take = max(1, cmax_conf // 2)

        if (mtm_stage or "").lower() == "final":
            short = list(candidates)
        else:
            short = _tail_biased_take(candidates, cmax_conf, recent_take)
    else:
        short = candidates[:C_MAX]

    if src == "MTM":
        prompt = (
            "Compose EPISODIC memory notes for a conversational agent.\n"
            f"Goal: several distinct episodes (not one summary) so the agent knows WHAT happened and WHEN, "
            f"within ≈ {max_tokens} tokens.\n"
            "Rules:\n"
            "- Keep only items clearly related to the current topic.\n"
            "- Preserve key facts, decisions, user preferences, constraints, and commitments; avoid vague paraphrase.\n"
            "- Respect chronology (older → newer). Use [YYYY-MM-DD] at the start when a date is known.\n"
            "- If no date is known for an item, omit the date (never invent one).\n"
            "- When important, note who said what (User vs Assistant); short quotes are allowed.\n"
            "- Preserve the direction of actions and feelings (who did what to whom); never invert it.\n"
            "- Write episodes as neutral background notes, not as messages to the user.\n"
            "- Each episode is a separate paragraph without numbering or extra labels.\n"
            "- If different people or situations are mixed, keep only episodes that clearly match the CURRENT USER REQUEST.\n"
            "- If unsure that a fragment refers to the same person or situation, exclude it.\n"
            "- Do NOT invent information.\n"
            "-----\n"
            f"[QUERY/TOPIC]\n{query}\n"
            "-----\n"
            "[CANDIDATES]\n" + "\n---\n".join(short)
        )
    else:
        prompt = (
            "From the candidates below, select and merge only the fragments most relevant to the user's query "
            f"into a single coherent snippet (max ≈ {max_tokens} tokens).\n"
            "If relevance is similar, prefer more recent fragments.\n"
            "Return ONLY the merged text (no numbering, headings, or labels).\n"
            "Keep the original language if consistent; otherwise use the query language.\n"
            "-----\n"
            f"[SOURCE]\n{source}\n"
            "-----\n"
            f"[QUERY]\n{query}\n"
            "-----\n"
            "[CANDIDATES]\n" + "\n---\n".join(short)
        )
    try:
        _t0 = time_module.perf_counter()
        timeout_sel = _NANO_TIMEOUT_MTM if src == "MTM" else _NANO_TIMEOUT_LTM
        chosen_model = model or settings.BASE_MODEL
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=chosen_model,
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

async def compose_mtm_snippet_pyramid(
    query: str,
    mtm_lines: List[str],
    max_tokens: int,
) -> str:
    if not query or not mtm_lines:
        return ""

    recent_cap = settings.MTM_RECENT_TAIL_TOKENS
    stage1_out = settings.MTM_STAGE1_MAX_TOKENS
    stage1_model = settings.BASE_MODEL
    final_model = settings.REASONING_MODEL
    jitter_ms = settings.MTM_PARALLEL_JITTER_MS
    batch_min = settings.MTM_LLM_BATCH

    base_tail = _tail_by_tokens(mtm_lines, recent_cap)
    if not base_tail:
        return ""

    pairs = _pairize_mtm(base_tail)
    if not pairs:
        return ""

    total_tokens = _safe_approx_total_tokens(base_tail)
    dyn_parallel = _dynamic_parallel_by_tokens(total_tokens)

    try:
        mtm_cap = settings.MTM_PARALLEL
    except Exception:
        mtm_cap = dyn_parallel

    try:
        api_cap = settings.OPENAI_MAX_CONCURRENT_REQUESTS
    except Exception:
        api_cap = 100

    max_batches_cfg = settings.MTM_MAX_BATCHES

    desired_chunks = max(1, min(dyn_parallel, len(pairs), max_batches_cfg))

    ideal_chunk_size = max(
        max(1, batch_min),
        (len(pairs) + desired_chunks - 1) // desired_chunks,
    )

    raw_batches: List[List[str]] = [
        pairs[s:s + ideal_chunk_size]
        for s in range(0, len(pairs), ideal_chunk_size)
    ]
    if not raw_batches:
        return ""

    if len(raw_batches) > max_batches_cfg:
        keep_tail = min(max_batches_cfg // 3, 8)
        tail = raw_batches[-keep_tail:] if keep_tail > 0 else []
        head = raw_batches[:len(raw_batches) - len(tail)]
        need_head = max_batches_cfg - len(tail)

        head_pick: List[List[str]] = []
        if need_head > 0 and head:
            step = max(1, len(head) // max(1, need_head))
            head_pick = head[::step][:max(0, need_head)]

        batches: List[List[str]] = (head_pick + tail) or raw_batches[-max_batches_cfg:]
    else:
        batches = raw_batches

    parallel = max(1, min(dyn_parallel, mtm_cap, api_cap, len(batches)))
    _log_parallel_plan("MTM pyramid", base_tail, parallel)

    def _annotate_with_date(items: Iterable[str]) -> List[str]:
        out: List[str] = []
        for ln in items:
            ts, rest = _parse_unix_ts_prefix(ln)
            date_tag = _fmt_date_utc(ts)
            out.append(f"[{date_tag}] {rest}" if date_tag else rest)
        return out

    sem = asyncio.Semaphore(parallel)

    async def _run_batch(ch: List[str]) -> str:
        await asyncio.sleep(random.randint(0, jitter_ms) / 1000.0)
        ann = _annotate_with_date(ch)
        async with sem:
            return await select_snippets_via_nano(
                "MTM", query, ann, stage1_out,
                model=stage1_model, mtm_stage="stage1",
            )

    logger.info(
        "MTM pyramid: tail_tokens≈%d batches=%d (capped<=%d) "
        "batch_size≈%d stage1_out=%d parallel=%d",
        total_tokens,
        len(batches),
        max_batches_cfg,
        ideal_chunk_size,
        stage1_out,
        parallel,
    )

    stage1 = await asyncio.gather(
        *(_run_batch(b) for b in batches),
        return_exceptions=True,
    )

    cands_raw: List[str] = []
    for res in stage1:
        if isinstance(res, Exception):
            logger.warning("MTM Stage-1 batch failed: %r", res)
            continue
        if res and res.strip():
            cands_raw.append(res.strip())

    seen: set[str] = set()
    cands: List[str] = []
    for s in cands_raw:
        k = s.casefold()
        if k in seen:
            continue
        seen.add(k)
        cands.append(s)

    if not cands:
        return ""

    final = await select_snippets_via_nano("MTM", query, cands, max_tokens, model=final_model, mtm_stage="final")
    return final or ""

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
    offset = random.randrange(n)
    for k in range(max_n):
        idx = int((k * step + offset) % n)
        if idx >= n:
            idx = n - 1
        if idx not in seen:
            out.append(lines[idx]); seen.add(idx)
    return out

async def preselect_mtm_candidates(query: str, mtm_lines: List[str], topn: int = 40) -> List[str]:

    if not query or not mtm_lines:
        return []

    try:
        recent_cap = settings.MTM_RECENT_TAIL_TOKENS
    except Exception:
        recent_cap = 80_000

    base = _tail_by_tokens(mtm_lines, recent_cap)
    base = [s for s in base if approx_tokens(s) >= 3]
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
        "Current dialogue topic and user intent:\n"
        f"{query}\n\n"
        "Task: pick MTM fragments that are clearly about this topic, especially the CURRENT USER REQUEST (if present).\n"
        "- Pay attention to who did what to whom; prefer fragments with the same direction of actions and feelings.\n"
        "- Prefer fragments with user preferences, decisions, plans, or non-obvious facts that can affect the next reply.\n"
        "- If relevance is similar, prefer more recent items (by [YYYY-MM-DD]).\n"
        "- Ignore generic chit-chat; focus on continuity of the current thread."
    )

    batch = settings.MTM_LLM_BATCH
    picked = await rerank_with_llm(llm_query, annotated, topk=topn, batch=batch)
    return picked[:topn]

async def compose_mtm_snippet(query: str, mtm_lines: List[str], max_tokens: int) -> str:

    use_pyramid = bool(getattr(settings, "MTM_PYRAMID_ENABLED", True))
    trigger_tokens = settings.MTM_PYRAMID_TRIGGER_TOKENS

    if use_pyramid:
        try:
            approx_total = sum(approx_tokens(x) for x in mtm_lines)
        except Exception:
            approx_total = sum(approx_tokens(x) for x in mtm_lines[:2000])

        if approx_total >= trigger_tokens:
            return await compose_mtm_snippet_pyramid(query, mtm_lines, max_tokens)

    cands = await preselect_mtm_candidates(query, mtm_lines, topn=settings.MTM_STAGE2_TOPN)
    if not cands:
        return ""
    logger.info(
        "Retrieval[MTM LLM]: picked=%d → nano_max=%d, approx_tokens_candidates≈%d",
        len(cands), max_tokens, sum(approx_tokens(x) for x in cands)
    )
    return await select_snippets_via_nano(
        "MTM", query, cands, max_tokens, mtm_stage="final"
    )

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

        prompt = (
            "Based on the recent user–assistant messages, summarize:\n"
            "- what the conversation is about\n"
            "- what the user is trying to achieve.\n"
            "Return a single short topic phrase (max 12 words) in the language of the messages, "
            "without quotes or ending punctuation.\n"
            "-----\n" + "\n".join(lines)
        )
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.BASE_MODEL,
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
            "Current dialogue topic and intent:\n"
            f"{query}\n\n"
            "Alternative phrasings (for coverage):" + alt_block + "\n\n"
            "Task: choose long-term memory fragments that are clearly relevant to this topic and that can personalize "
            "or constrain the next reply.\n"
            "- Prefer durable facts, preferences, constraints, and commitments.\n"
            "- If relevance is similar, prefer more recent or more specific fragments.\n"
            "- Ignore generic small talk."
        )

        try:
            batch = settings.LTM_LLM_BATCH
        except Exception:
            batch = 30

        picked = await rerank_with_llm(llm_query, pool, topk=topn, batch=batch)

    if not picked:
        logger.info("Retrieval[LTM LLM-only]: no candidates returned; yielding empty snippet")
        return ""

    return await select_snippets_via_nano("LTM", query, picked, max_tokens)