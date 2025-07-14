cat >app/bot/utils/user_mode.py<< EOF
#app/bot/utils/user_mode.py
from enum import Enum

from app.bot.components.constants import redis_client

class UserMode(str, Enum):
    
    AUTO = "auto"
    ON_TOPIC = "on_topic"
    OFF_TOPIC = "off_topic"

async def get_user_mode(uid: int) -> UserMode:

    if not redis_client:
        return UserMode.AUTO

    raw = await redis_client.get(f"user_mode:{uid}")
    if raw is None:
        return UserMode.AUTO
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return UserMode(raw)
    except ValueError:
        return UserMode.AUTO

async def set_user_mode(uid: int, mode: UserMode) -> None:

    if not redis_client:
        return
    await redis_client.set(f"user_mode:{uid}", mode.value)
EOF