cat > app/tasks/__init__.py << EOF
#app/tasks/__init__.py
from .celery_app import celery
from .message import process_message, summarize_old
from .scheduler import start_scheduler, sched

__all__ = [
    "celery",
    "process_message",
    "summarize_old",
    "start_scheduler",
    "sched"
]
EOF