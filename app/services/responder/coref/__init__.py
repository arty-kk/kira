cat >app/services/responder/coref/__init__.py<< 'EOF'
#app/services/responder/coref/__init__.py
from .needs_coref import needs_coref
from .resolve_coref import resolve_coref

__all__ = [
    "needs_coref", 
    "resolve_coref"
]
EOF