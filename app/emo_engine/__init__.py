cat >app/emo_engine/__init__.py<< EOF
#app/emo_engine/__init__.py
from .registry import get_persona

__all__ = [
    "get_persona",
]
EOF