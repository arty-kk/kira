cat > app/tasks/message.py << EOF
#app/tasks/message.py
import asyncio 
import threading
import logging

from celery.utils.log import get_task_logger
from aiogram.exceptions import TelegramBadRequest

from .celery_app import celery
from app.services.responder import respond_to_user
from app.clients.telegram_client import get_bot
from app.config import settings

logger = get_task_logger(__name__)

_ASYNCIO_LOOP = asyncio.new_event_loop()
threading.Thread(target=_ASYNCIO_LOOP.run_forever, daemon=True).start()

async def _process_message_async(
    chat_id: int,
    text: str,
    placeholder_id: int | None = None,
    reply_to_message_id: int | None = None,
    user_id: int | None = None,
    remaining: int | None = None,
    username: str | None = None,
) -> None:

    bot = get_bot()

    stop_event = asyncio.Event()

    async def _typing_loop():
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(chat_id, "typing")
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(_typing_loop())

    try:
        reply = await respond_to_user(text, chat_id, user_id)
        suffix = f"\n\n📊 <b>{remaining}</b> requests left." if remaining is not None else ""
        full_text = reply + suffix

        if placeholder_id:
            try:
                await bot.edit_message_text(
                    full_text,
                    chat_id=chat_id,
                    message_id=placeholder_id,
                    parse_mode="HTML",
                )
            except TelegramBadRequest:
                await bot.send_message(chat_id, full_text, parse_mode="HTML")
        else:
            if reply_to_message_id:
                await bot.send_message(
                    chat_id,
                    text=full_text,
                    reply_to_message_id=reply_to_message_id,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            else:
                await bot.send_message(
                    chat_id,
                    full_text,
                    parse_mode="HTML",
                )
    except Exception as exc:
        logger.exception("Error in _process_message_async", exc_info=exc)
    finally:
        stop_event.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass


@celery.task(
    name="process_message",
    acks_late=True,
    time_limit=60,
)
def process_message(
    chat_id: int,
    text: str,
    placeholder_id: int | None = None,
    reply_to_message_id: int | None = None,
    user_id: int | None = None,
    remaining: int | None = None,
    username: str | None = None,
) -> None:
    try:
        future = asyncio.run_coroutine_threadsafe(
            _process_message_async(
                chat_id=chat_id,
                text=text,
                placeholder_id=placeholder_id,
                reply_to_message_id=reply_to_message_id,
                user_id=user_id,
                remaining=remaining,
                username=username,
            ),
            _ASYNCIO_LOOP
        )
        future.result(timeout=60)
    except Exception as exc:
        logger.exception("process_message failed", exc_info=exc)
        raise


@celery.task(
    name="summarize_old",
    acks_late=True,
    time_limit=120,
)
def summarize_old(chat_id: int, length: int) -> None:
    import json
    import time as time_module
    from app.core.memory import get_redis, _key_msgs, _key_summary, MEMORY_TTL
    from app.clients.openai_client import _call_openai_with_retry

    logger = logging.getLogger(__name__)

    async def _summarize():
        redis = get_redis()
        key_msgs = _key_msgs(chat_id)
        key_sum = _key_summary(chat_id)

        old_summary = await redis.get(key_sum) or ""
        half = length // 2
        msgs = await redis.lrange(key_msgs, 0, half - 1)

        texts: list[str] = []

        for m in msgs:
            try:
                obj = json.loads(m)
            except json.JSONDecodeError:
                logger.warning("Bad JSON during summarization for chat %s: %s", chat_id, m)
                continue

            if obj.get("role") == "assistant":
                speaker = settings.BOT_PERSONA_NAME
            else:
                speaker = f"User#{obj.get('user_id', '')}".rstrip('#')

            texts.append(f"{speaker}: {obj['content']}")

        if not texts and not old_summary:
            return

        prompt = "\n".join([
            "Summarize the following conversation history:\n",
            old_summary,
            "" if not old_summary else "",
            "\n".join(texts),
        ])

        try:
            sys_prompt = (
                "You are a assistant that creates concise and factual summaries of conversation."
            )
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    model=settings.REASONING_MODEL,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=330,
                    temperature=0.0,
                ),
                timeout=60.0,
            )
            new_summary = resp.choices[0].message.content.strip()
        except Exception as e:
            logger.exception("summarize_old: OpenAI summarization failed for chat %s", chat_id, exc_info=e)
            return

        async with redis.pipeline() as pipe:
            pipe.set(key_sum, new_summary, ex=MEMORY_TTL)
            pipe.ltrim(key_msgs, half, -1)
            await pipe.execute()

        logger.info("summarize_old: chat %s summary updated", chat_id)

    try:
        future = asyncio.run_coroutine_threadsafe(_summarize(), _ASYNCIO_LOOP)
        future.result(timeout=110)
    except Exception as exc:
        logger.exception("summarize_old task failed for chat %s", chat_id, exc_info=exc)
        raise
EOF