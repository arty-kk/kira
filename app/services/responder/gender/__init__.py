cat >app/services/responder/gender/__init__.py<< EOF
#app/services/responder/gender/__init__.py
from .gender_detector import detect_gender

__all__ = ["detect_gender"]
EOF