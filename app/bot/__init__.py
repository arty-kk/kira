cat >app/bot/__init__.py<< 'EOF'
#app/bot/__init__.py
from .components.webhook import start_bot

__all__ = [
    "start_bot",
]
EOF