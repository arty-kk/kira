cat >app/services/responder/rag/__init__.py<< 'EOF'
#app/services/responder/rag/__init__.py
from .topic_detector import is_on_topic
from .knowledge_proc import get_relevant, _init_kb, _KB_ENTRIES
from .relevance import relevant_enough

__all__ = [
    "is_on_topic",
    "get_relevant",
    "_init_kb",
    "_KB_ENTRIES",
    "relevant_enough",
]
EOF