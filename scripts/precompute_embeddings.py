cat >scripts/precompute_embeddings.py<< EOF
#scripts/precompute_embeddings.py
import os
import json
import re
import argparse
import openai

from typing import List
from pathlib import Path
from dotenv import load_dotenv
from tqdm import tqdm


load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")

parser = argparse.ArgumentParser(
    description="Precompute embeddings for a given knowledge JSON"
)
parser.add_argument(
    "--kb-file",
    dest="kb_file",
    type=str,
    required=True,
    help="Path to the JSON with knowledge"
)
parser.add_argument(
    "--out-file",
    dest="out_file",
    type=str,
    required=True,
    help="Where to save precomputed embeddings"
)
args = parser.parse_args()

KB_PATH = Path(args.kb_file)
OUT_PATH = Path(args.out_file)

EMB_DIR = OUT_PATH.parent
EMB_DIR.mkdir(parents=True, exist_ok=True)

print(f"→ Reading KB from: {KB_PATH}")
print(f"→ Writing embeddings to: {OUT_PATH}")
print(f"→ Using embedding model: {MODEL}")

SENT_SPLIT = re.compile(r'(?<=[.!?])\s+')
def _split(text: str, size: int = 1000) -> List[str]:
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

with open(KB_PATH, encoding="utf-8") as f:
    raw_text = f.read()
    cleaned = re.sub(r',\s*([\]\}])', r'\1', raw_text)
    cleaned = "\n".join(line for line in cleaned.splitlines() if line.strip())
    items = json.loads(cleaned)

entries = []
for item in items:
    orig_id  = item.get("id", "")
    category = item.get("category", "general")
    tags     = item.get("tags", [])
    text     = item.get("text", "").strip()
    if not text:
        continue
    for chunk in _split(text):
        entries.append({
            "id": orig_id,
            "category": category,
            "tags": tags,
            "text": chunk,
        })

BATCH_SIZE = 1000
embedded = []
for i in tqdm(range(0, len(entries), BATCH_SIZE), desc="Embedding batches"):
    batch = entries[i : i + BATCH_SIZE]
    texts = [e["text"] for e in batch]
    resp = openai.embeddings.create(model=MODEL, input=texts)
    for emb_obj, meta in zip(resp.data, batch):
        e = meta.copy()
        e["emb"] = emb_obj.embedding
        embedded.append(e)

with open(OUT_PATH, "w", encoding="utf-8") as f:
    json.dump(embedded, f, ensure_ascii=False, indent=2)

print(f"Saved {len(embedded)} embedded chunks to {OUT_PATH}")
EOF