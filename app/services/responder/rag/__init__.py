#app/services/responder/rag/__init__.py
from .knowledge_proc import _init_kb, _KB_ENTRIES
from .relevance import is_relevant

__all__ = [
    "is_relevant",
    "_init_kb",
    "_KB_ENTRIES",
]