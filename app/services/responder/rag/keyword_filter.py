cat >app/services/responder/rag/keyword_filter.py<< 'EOF'
#app/services/responder/rag/keyword_filter.py
import json
import logging
import re

from pathlib import Path
from flashtext import KeywordProcessor

from app.config import settings

logger = logging.getLogger(__name__)


def _build_processor() -> KeywordProcessor:

    raw_path = Path(__file__).resolve().parent / settings.KNOWLEDGE_ON_FILE
    if not raw_path.is_file():
        logger.error("Knowledge file not found at %s", raw_path)
        raise FileNotFoundError(f"Knowledge file not found: {raw_path}")

    raw_text = raw_path.read_text(encoding="utf-8")
    cleaned = re.sub(r',\s*([}\]])', r'\1', raw_text)
    entries = json.loads(cleaned)
    if not isinstance(entries, list):
        raise ValueError(f"Knowledge file must contain a JSON list, got {type(entries)}")

    proc = KeywordProcessor(case_sensitive=False)
    for idx, item in enumerate(entries):
        if not isinstance(item, dict):
            logger.warning("Skipping non-dict entry at index %d", idx)
            continue
        for tag in item.get("tags", []):
            if isinstance(tag, str):
                proc.add_keyword(tag.strip())
        cat = item.get("category")
        if isinstance(cat, str):
            proc.add_keyword(cat.strip())

    count = len(proc.get_all_keywords())
    logger.info(
        "keyword_filter: loaded %d entries → %d keywords from %s",
        len(entries), count, raw_path
    )
    return proc

KEYWORD_PROCESSOR: KeywordProcessor = _build_processor()

def get_keyword_processor() -> KeywordProcessor:

    return KEYWORD_PROCESSOR
EOF