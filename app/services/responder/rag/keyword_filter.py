cat >app/services/responder/rag/keyword_filter.py<< EOF
#app/services/responder/rag/keyword_filter.py
import json, logging, re

from pathlib import Path
from typing import Optional
from flashtext import KeywordProcessor

from app.config import settings

logger = logging.getLogger(__name__)

_keyword_processor: Optional[KeywordProcessor] = None

def get_keyword_processor() -> KeywordProcessor:

    global _keyword_processor
    if _keyword_processor is None:
        proc = KeywordProcessor(case_sensitive=False)

        raw_path = Path(__file__).resolve().parent / settings.KNOWLEDGE_ON_FILE
        if not raw_path.exists():
            logger.error("Knowledge file not found at %s", raw_path)
            raise FileNotFoundError(f"Knowledge file not found: {raw_path}")

        try:
            raw_text = raw_path.read_text(encoding="utf-8")
            cleaned_text = re.sub(r',\s*([}\]])', r'\1', raw_text)
            cleaned_text = "\n".join(line for line in cleaned_text.splitlines() if line.strip())
            entries = json.loads(cleaned_text)
        except Exception:
            logger.exception("Failed to load knowledge file from %s", raw_path)
            raise

        for item in entries:
            for tag in item.get("tags", []):
                proc.add_keyword(tag)
            cat = item.get("category")
            if isinstance(cat, str):
                proc.add_keyword(cat)

        count = len(proc.get_all_keywords())
        logger.info("keyword_filter: loaded %d keywords from %s", count, raw_path)

        _keyword_processor = proc

    return _keyword_processor
EOF