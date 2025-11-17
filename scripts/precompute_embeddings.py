#scripts/precompute_embeddings.py
import os
import json
import re
import time
import argparse
import logging
import openai
import threading
import tiktoken
import numpy as np

from pathlib import Path
from typing import List, Tuple, Dict, Set
from dotenv import load_dotenv
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")

parser = argparse.ArgumentParser(description="Precompute embeddings for a given knowledge JSON")
parser.add_argument("--kb-file", "-k", dest="kb_file", type=str, required=True, help="Path to the JSON with knowledge")
parser.add_argument("--out-file", "-o", dest="out_file", type=str, required=True, help="Where to save precomputed embeddings")
parser.add_argument("--batch-size", "-b", dest="batch_size", type=int, default=1000, help="Max items per API call (fallback cap)")
parser.add_argument("--chunk-tokens", dest="chunk_tokens", type=int, default=int(os.getenv("CHUNK_TOKENS", "500")),
                    help="Target tokens per chunk (default: 500)")
parser.add_argument("--chunk-overlap", dest="chunk_overlap", type=int, default=int(os.getenv("CHUNK_OVERLAP", "50")),
                    help="Token overlap between chunks (default: 50)")
parser.add_argument("--batch-tokens-limit", dest="batch_tokens_limit", type=int,
                    default=int(os.getenv("BATCH_TOKENS_LIMIT", "100000")),
                    help="Max tokens per embeddings.create request across inputs (default: 100k)")
args = parser.parse_args()

BATCH_SIZE = args.batch_size
CHUNK_TOKENS = max(64, args.chunk_tokens)
CHUNK_OVERLAP = max(0, min(args.chunk_overlap, CHUNK_TOKENS // 2))
BATCH_TOKENS_LIMIT = max(8_000, args.batch_tokens_limit)

KB_PATH = Path(args.kb_file)
OUT_PATH = Path(args.out_file)
NPZ_PATH = OUT_PATH.with_suffix(".npz")  # === NPZ ===
CHECKPOINT = OUT_PATH.with_suffix(".checkpoint")
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

logger.info("→ Reading KB from: %s", KB_PATH)
logger.info("→ Writing embeddings JSON to: %s", OUT_PATH)
logger.info("→ Will also write NPZ snapshot to: %s", NPZ_PATH)  # === NPZ ===
logger.info("→ Using embedding model: %s", MODEL)
logger.info("→ Chunk tokens: %d (overlap=%d)", CHUNK_TOKENS, CHUNK_OVERLAP)
logger.info("→ Batch tokens limit: %d", BATCH_TOKENS_LIMIT)

def _encoding_for(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        return tiktoken.get_encoding("cl100k_base")

ENC = _encoding_for(MODEL)

def count_tokens(text: str) -> int:
    return len(ENC.encode(text or ""))

def split_by_tokens(text: str, limit: int, overlap: int) -> List[str]:
    ids = ENC.encode(text or "")
    n = len(ids)
    if n <= limit:
        return [text.strip()] if (text or "").strip() else []
    chunks: List[str] = []
    start = 0
    while start < n:
        end = min(n, start + limit)
        piece = ENC.decode(ids[start:end]).strip()
        if piece:
            chunks.append(piece)
        if end == n:
            break
        start = max(end - overlap, start + 1)
    return chunks

def load_kb_any(path: Path) -> List[dict]:
    data = path.read_text(encoding="utf-8")
    if not data.strip():
        raise ValueError(f"KB file is empty: {path}")
    data = data.lstrip("\ufeff")
    try:
        obj = json.loads(data)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict) and isinstance(obj.get("items"), list):
            return obj["items"]
    except json.JSONDecodeError:
        pass
    no_comments = re.sub(r"^\s*//.*$", "", data, flags=re.M)
    no_comments = re.sub(r"/\*[^*]*\*+(?:[^/*][^*]*\*+)*/", "", no_comments, flags=re.S)
    no_trailing = re.sub(r",\s*([\]\}])", r"\1", no_comments).strip()
    try:
        obj = json.loads(no_trailing)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict) and isinstance(obj.get("items"), list):
            return obj["items"]
    except json.JSONDecodeError:
        pass
    items = []
    jsonl_ok = True
    for line in data.splitlines():
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        if s.endswith(","):
            s = s[:-1]
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                items.append(obj)
            else:
                jsonl_ok = False
                break
        except Exception:
            jsonl_ok = False
            break
    if jsonl_ok and items:
        return items
    raise ValueError(f"KB file is not valid JSON/JSONL: {path}")

items = load_kb_any(KB_PATH)
valid_items = []
for i, it in enumerate(items):
    if not isinstance(it, dict):
        logger.warning("Skipping non-dict item at index %d: %r", i, type(it))
        continue
    txt = (it.get("text") or "").strip()
    if not txt:
        continue
    valid_items.append(it)
items = valid_items

entries = []
for item in items:
    text = item.get("text", "").strip()
    if not text:
        continue
    for chunk in split_by_tokens(text, CHUNK_TOKENS, CHUNK_OVERLAP):
        entries.append({
            "id":        item.get("id", ""),
            "category":  item.get("category", "general"),
            "tags":      item.get("tags", []),
            "text":      chunk,
            "_tok":      count_tokens(chunk),
        })
logger.info("→ Total chunks to embed: %d (avg %.1f tok/chunk)",
            len(entries),
            (sum(e["_tok"] for e in entries) / max(1,len(entries))))

embedded = []
if CHECKPOINT.exists():
    try:
        with open(CHECKPOINT, "r", encoding="utf-8") as ck:
            embedded = json.load(ck) or []
        logger.info("Resuming from checkpoint with %d items", len(embedded))
    except Exception:
        logger.warning("Checkpoint is unreadable, starting fresh.")
        embedded = []
done_ids = { f'{item.get("id","")}|{item.get("text","")}' for item in embedded }

batches: List[List[dict]] = []
cur: List[dict] = []
tok_sum = 0
for e in entries:
    if f'{e["id"]}|{e["text"]}' in done_ids:
        continue
    if (len(cur) >= BATCH_SIZE) or (tok_sum + e["_tok"] > BATCH_TOKENS_LIMIT):
        if cur:
            batches.append(cur)
        cur = [e]
        tok_sum = e["_tok"]
    else:
        cur.append(e)
        tok_sum += e["_tok"]
if cur:
    batches.append(cur)

logger.info("→ Prepared %d batches (≤%d items, ≤%d tokens each)",
            len(batches), BATCH_SIZE, BATCH_TOKENS_LIMIT)

sema = threading.Semaphore(int(os.getenv("EMBED_CONCURRENCY", "2")))
@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True
)
def embed_batch(texts: List[str]) -> List[List[float]]:
    with sema:
        resp = openai.embeddings.create(model=MODEL, input=texts)
    return [item.embedding for item in resp.data]

start_time = time.time()
with ThreadPoolExecutor(max_workers=2) as executor:
    tasks = []
    for batch in batches:
        future = executor.submit(embed_batch, [e["text"] for e in batch])
        tasks.append((future, batch))

    for future, batch in tqdm(tasks, desc="Embedding"):
        try:
            embs = future.result()
        except Exception:
            logger.exception("Embedding batch failed, skipping")
            continue
        for meta, emb in zip(batch, embs):
            meta.pop("_tok", None)
            meta["emb"] = emb
            embedded.append(meta)

        tmp_ck = CHECKPOINT.with_suffix(".checkpoint.tmp")
        with open(tmp_ck, "w", encoding="utf-8") as ck:
            json.dump(embedded, ck, ensure_ascii=False, indent=2)
        tmp_ck.replace(CHECKPOINT)

elapsed = time.time() - start_time
logger.info("→ Completed embedding %d chunks in %.1fs", len(embedded), elapsed)

tmp_path = OUT_PATH.with_suffix(OUT_PATH.suffix + ".tmp")
with open(tmp_path, "w", encoding="utf-8") as f:
    f.write("[\n")
    for i, item in enumerate(embedded):
        json.dump(item, f, ensure_ascii=False)
        if i < len(embedded) - 1:
            f.write(",\n")
    f.write("\n]\n")
tmp_path.replace(OUT_PATH)
if CHECKPOINT.exists():
    CHECKPOINT.unlink()
logger.info("✅ Saved %d embedded chunks to %s", len(embedded), OUT_PATH)

try:
    if not embedded:
        raise RuntimeError("No embedded data to snapshot")

    dim0 = len(embedded[0]["emb"])
    rows = []
    ids, texts = [], []
    skipped = 0
    for it in embedded:
        e = it.get("emb")
        if not isinstance(e, (list, tuple)) or len(e) != dim0:
            skipped += 1
            continue
        rows.append(e)
        ids.append(str(it.get("id", "")))
        texts.append(str(it.get("text", "")))

    if not rows:
        raise RuntimeError("No valid embeddings for NPZ snapshot")

    M = np.asarray(rows, dtype=np.float32)
    mean = M.mean(axis=0).astype(np.float32)
    M = M - mean
    norms = np.linalg.norm(M, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    E = M / norms

    E = np.ascontiguousarray(np.nan_to_num(E, nan=0.0, posinf=0.0, neginf=0.0), dtype=np.float32)
    mean = np.ascontiguousarray(np.nan_to_num(mean, nan=0.0, posinf=0.0, neginf=0.0), dtype=np.float32)

    file_model = OUT_PATH.stem
    if file_model.startswith("knowledge_embedded_"):
        file_model = file_model[len("knowledge_embedded_"):]
    
    np.savez_compressed(
        NPZ_PATH,
        E=E,
        mean=mean,
        ids=np.asarray(ids, dtype=object),
        texts=np.asarray(texts, dtype=object),
        meta=np.asarray({"model": file_model, "api_model": MODEL, "dim": E.shape[1], "created": int(time.time())}, dtype=object),
    )
    logger.info("✅ NPZ snapshot saved: %s (E=%s, skipped=%d)", NPZ_PATH, E.shape, skipped)
except Exception:
    logger.exception("Failed to write NPZ snapshot")

def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().casefold())

try:
    kw_by_id: Dict[str, List[str]] = {}
    texts_by_id: Dict[str, str] = {}
    all_unique_kws: List[str] = []
    seen_kw: Set[str] = set()

    for it in items:
        eid = str(it.get("id", "") or "")
        etext = str(it.get("text", "") or "")
        if not eid or not etext:
            continue
        texts_by_id[eid] = etext

        kws: List[str] = []
        tags = it.get("tags") or []
        if isinstance(tags, list):
            for t in tags:
                if isinstance(t, str):
                    t2 = _norm_ws(t)
                    if t2:
                        kws.append(t2)

        if not kws:
            t_norm = _norm_ws(etext)
            if t_norm:
                kws = [t_norm]

        kws = list(dict.fromkeys(kws))
        kw_by_id[eid] = kws
        for s in kws:
            if s not in seen_kw:
                seen_kw.add(s)
                all_unique_kws.append(s)

    logger.info("→ Unique keywords collected: %d", len(all_unique_kws))

    kw_emb: Dict[str, List[float]] = {}
    if all_unique_kws:
        batched = [all_unique_kws[i:i+BATCH_SIZE] for i in range(0, len(all_unique_kws), BATCH_SIZE)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            futs = [(executor.submit(embed_batch, chunk), chunk) for chunk in batched]
            for fut, chunk in tqdm(futs, desc="Embedding tags"):
                try:
                    vecs = fut.result()
                except Exception:
                    logger.exception("Embedding tags batch failed, skipping batch")
                    continue
                for s, v in zip(chunk, vecs):
                    kw_emb[s] = [float(x) for x in v]

    ids_tags: List[str] = []
    texts_tags: List[str] = []
    kws_per_item: List[List[str]] = []
    rows_tags: List[List[float]] = []

    dim_kw = None
    skipped_items = 0
    for eid, kws in kw_by_id.items():
        vec_sum = None
        cnt = 0
        for s in kws:
            v = kw_emb.get(s)
            if v is None:
                continue
            if vec_sum is None:
                vec_sum = [float(x) for x in v]
                dim_kw = len(vec_sum) if dim_kw is None else dim_kw
            else:
                if len(v) != dim_kw:
                    continue
                for i in range(dim_kw):
                    vec_sum[i] += v[i]
            cnt += 1
        if not vec_sum or cnt == 0:
            skipped_items += 1
            continue
        inv = 1.0 / float(cnt)
        for i in range(len(vec_sum)):
            vec_sum[i] *= inv
        vv = np.asarray(vec_sum, dtype=np.float32)
        nrm = float(np.linalg.norm(vv))
        if not np.isfinite(nrm) or nrm == 0.0:
            skipped_items += 1
            continue
        vv = vv / nrm
        rows_tags.append(vv.tolist())
        ids_tags.append(eid)
        texts_tags.append(texts_by_id.get(eid, ""))
        kws_per_item.append(kws)

    if not rows_tags:
        logger.warning("No tag vectors produced — skipping TAGS NPZ/JSON.")
    else:
        file_model = OUT_PATH.stem
        if file_model.startswith("knowledge_embedded_"):
            file_model = file_model[len("knowledge_embedded_"):]
        TAGS_JSON = OUT_PATH.parent / f"tags_embedded_{file_model}.json"
        tag_entries = []
        for eid, txt, kws, vec in zip(ids_tags, texts_tags, kws_per_item, rows_tags):
            tag_entries.append({"id": eid, "text": txt, "keywords": kws, "vec": vec})
        tmp_tags_json = TAGS_JSON.with_suffix(TAGS_JSON.suffix + ".tmp")
        with open(tmp_tags_json, "w", encoding="utf-8") as f:
            json.dump(tag_entries, f, ensure_ascii=False, indent=2)
        tmp_tags_json.replace(TAGS_JSON)
        logger.info("✅ Saved tag vectors JSON: %s (items=%d, skipped=%d)", TAGS_JSON, len(tag_entries), skipped_items)

        E_tags = np.asarray(rows_tags, dtype=np.float32)
        TAGS_NPZ = OUT_PATH.parent / f"tags_embedded_{file_model}.npz"
        np.savez_compressed(
            TAGS_NPZ,
            E=E_tags,
            ids=np.asarray(ids_tags, dtype=object),
            texts=np.asarray(texts_tags, dtype=object),
            kws=np.asarray(kws_per_item, dtype=object),
            meta=np.asarray({"model": file_model, "api_model": MODEL, "dim": E_tags.shape[1], "created": int(time.time())}, dtype=object),
        )
        logger.info("✅ TAGS NPZ saved: %s (E=%s)", TAGS_NPZ, E_tags.shape)

except Exception:
    logger.exception("Failed to build/save TAG embeddings snapshot")