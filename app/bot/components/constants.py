cat > app/bot/components/constants.py << 'EOF'
# app/bot/components/constants.py
from pathlib import Path
from redis.asyncio import Redis

from app.config import settings
from app.core.memory import get_redis

LANG_FILE = Path(__file__).parent.parent / "i18n" / "welcome_messages.json"

redis_client: Redis = get_redis()

BOT_ID: int = settings.TELEGRAM_BOT_ID
BOT_USERNAME: str = settings.TELEGRAM_BOT_USERNAME

WELCOME_MESSAGES: dict[str, str] = {}
EOF