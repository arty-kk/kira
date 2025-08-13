cat >scripts/precompute_embeddings.py<< 'EOF'
#scripts/precompute_embeddings.py
import os
import json
import re
import time
import argparse
import logging
import openai
import threading

from pathlib import Path
from typing import List
from dotenv import load_dotenv
from tqdm import tqdm
from openai._exceptions import RateLimitError, OpenAIError
from concurrent.futures import ThreadPoolExecutor, as_completed
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")


parser = argparse.ArgumentParser(
    description="Precompute embeddings for a given knowledge JSON"
)
parser.add_argument(
    "--kb-file", "-k",
    dest="kb_file",
    type=str,
    required=True,
    help="Path to the JSON with knowledge"
)
parser.add_argument(
    "--out-file", "-o",
    dest="out_file",
    type=str,
    required=True,
    help="Where to save precomputed embeddings"
)
parser.add_argument(
    "--batch-size", "-b",
    dest="batch_size",
    type=int,
    default=1000,
    help="Number of texts per API call (default: 1000)"
)
args = parser.parse_args()

BATCH_SIZE = args.batch_size

KB_PATH = Path(args.kb_file)
OUT_PATH = Path(args.out_file)
CHECKPOINT = OUT_PATH.with_suffix(".checkpoint")


OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

logger.info("→ Reading KB from: %s", KB_PATH)
logger.info("→ Writing embeddings to: %s", OUT_PATH)
logger.info("→ Using embedding model: %s", MODEL)
logger.info("→ Batch size: %d", BATCH_SIZE)


SENT_SPLIT = re.compile(r'(?<=[\.!?])\s+')
def split_text(text: str, size: int = 1000) -> List[str]:
    sentences = SENT_SPLIT.split(text)
    chunks, current, curr_len = [], [], 0
    for sent in sentences:
        s = sent.strip()
        if not s:
            continue
        slen = len(s)
        if slen > size:
            if current:
                chunks.append(" ".join(current))
                current, curr_len = [], 0
            chunks.append(s)
        elif current and curr_len + slen + 1 > size:
            chunks.append(" ".join(current))
            current, curr_len = [s], slen + 1
        else:
            current.append(s)
            curr_len += slen + 1
    if current:
        chunks.append(" ".join(current))
    return chunks


if CHECKPOINT.exists():
    with open(CHECKPOINT, "r", encoding="utf-8") as ck:
        try:
            done = json.load(ck)
            done_ids = { item["id"] + "|" + item["text"] for item in done }
        except Exception:
            done_ids = set()
else:
    done_ids = set()


with open(KB_PATH, encoding="utf-8") as f:
    raw = f.read()
    cleaned = re.sub(r',\s*([\]\}])', r'\1', raw)
    cleaned = "\n".join(line for line in cleaned.splitlines() if line.strip())
    items = json.loads(cleaned)


entries = []
for item in items:
    text = item.get("text", "").strip()
    if not text:
        continue
    for chunk in split_text(text):
        entries.append({
            "id":        item.get("id", ""),
            "category":  item.get("category", "general"),
            "tags":      item.get("tags", []),
            "text":      chunk,
        })
logger.info("→ Total chunks to embed: %d", len(entries))

sema = threading.Semaphore(int(os.getenv("EMBED_CONCURRENCY", "2")))
@retry(
    retry=retry_if_exception_type((RateLimitError, OpenAIError, Exception)),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True
)
def embed_batch(texts: List[str]) -> List[List[float]]:
    with sema:
        resp = openai.embeddings.create(model=MODEL, input=texts)
    return [item.embedding for item in resp.data]


embedded = []
start_time = time.time()
with ThreadPoolExecutor(max_workers=2) as executor:
    tasks = []
    for i in range(0, len(entries), BATCH_SIZE):
        batch = entries[i : i + BATCH_SIZE]
        batch_ids = {item["id"] + "|" + item["text"] for item in batch}
        if batch_ids & done_ids:
            continue
        future = executor.submit(embed_batch, [e["text"] for e in batch])
        tasks.append((future, batch))

    for future, batch in tqdm(tasks, desc="Embedding"):
        try:
            embs = future.result()
        except Exception:
            logger.exception("Embedding batch failed, skipping")
            continue
        for meta, emb in zip(batch, embs):
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
EOF