cat >app/services/responder/rag/keyword_filter.py<< 'EOF'
# app/services/responder/rag/keyword_filter.py
import json
import logging
import re
from pathlib import Path
from typing import Dict, Optional

from flashtext import KeywordProcessor
from app.config import settings

logger = logging.getLogger(__name__)

_PROCESSORS: Dict[str, KeywordProcessor] = {}

def _build_processor_from_file(path: Path) -> KeywordProcessor:
    if not path.is_file():
        logger.warning("Knowledge keywords file not found at %s", path)
        return KeywordProcessor(case_sensitive=False)

    raw_text = path.read_text(encoding="utf-8")

    cleaned = re.sub(r',\s*([}\]])', r'\1', raw_text)
    try:
        data = json.loads(cleaned)
    except Exception:
        logger.exception("Failed to parse keyword file %s", path)
        return KeywordProcessor(case_sensitive=False)

    if not isinstance(data, list):
        logger.error("Keyword file must be a JSON list, got %s", type(data))
        return KeywordProcessor(case_sensitive=False)

    proc = KeywordProcessor(case_sensitive=False)
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            logger.warning("Skipping non-dict entry at index %d", idx)
            continue
        for tag in item.get("tags", []):
            if isinstance(tag, str) and tag.strip():
                proc.add_keyword(tag.strip())
        cat = item.get("category")
        if isinstance(cat, str) and cat.strip():
            proc.add_keyword(cat.strip())

    logger.info(
        "keyword_filter: loaded %d items → %d keywords from %s",
        len(data), len(proc.get_all_keywords()), path
    )
    return proc


def get_keyword_processor(model: Optional[str] = None) -> KeywordProcessor:

    is_off = (model == getattr(settings, "OFFTOPIC_EMBEDDING_MODEL", None))
    key = "off" if is_off else "on"

    if key in _PROCESSORS:
        return _PROCESSORS[key]

    base_dir = Path(__file__).resolve().parent
    filename = (
        getattr(settings, "KNOWLEDGE_OFF_FILE", None) if is_off
        else getattr(settings, "KNOWLEDGE_ON_FILE", None)
    )

    if not filename:
        logger.warning(
            "No keyword filename configured for key=%s; returning empty processor",
            key
        )
        proc = KeywordProcessor(case_sensitive=False)
        _PROCESSORS[key] = proc
        return proc

    proc = _build_processor_from_file(base_dir / filename)
    _PROCESSORS[key] = proc
    return proc

EOF