#app/bot/utils/telegram_safe.py
import logging

from typing import Optional, Any, Dict

from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

from app.bot.components.constants import redis_client
from app.config import settings

logger = logging.getLogger(__name__)


def _strip_duplicates(explicit: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k not in explicit}

async def send_message_safe(
    bot,
    chat_id: int,
    text: str,
    *,
    parse_mode: Optional[str] = None,
    reply_markup=None,
    **kwargs,
) -> Optional["Message"]:
    try:
        kw = _strip_duplicates({"parse_mode": parse_mode, "reply_markup": reply_markup}, kwargs)
        return await bot.send_message(
            chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup, **kw
        )
    except TelegramForbiddenError:
        logger.info("send_message_safe: Forbidden chat=%s — marking blocked", chat_id)
        return None
    except TelegramBadRequest as e:
        if "reply" in str(e).lower() or "message to be replied" in str(e).lower():
            kw = _strip_duplicates({"parse_mode": parse_mode, "reply_markup": reply_markup}, kwargs)
            kw.pop("reply_to_message_id", None)
            try:
                return await bot.send_message(
                    chat_id, text, parse_mode=parse_mode,
                    reply_markup=reply_markup, **kw
                )
            except TelegramForbiddenError:
                logger.info("send_message_safe(no-reply): Forbidden chat=%s", chat_id)
                return None
            except TelegramBadRequest:
                try:
                    return await bot.send_message(
                        chat_id, text, reply_markup=reply_markup, **kw
                    )
                except TelegramForbiddenError:
                    logger.info("send_message_safe(plain,no-reply): Forbidden chat=%s", chat_id)
                    return None
                except Exception:
                    logger.exception("send_message_safe: fallback(no-reply) failed")
                    return None
        try:
            kw = _strip_duplicates({"reply_markup": reply_markup}, kwargs)
            return await bot.send_message(
                chat_id, text, reply_markup=reply_markup, **kw
            )
        except TelegramForbiddenError:
            logger.info("send_message_safe (fallback): Forbidden chat=%s", chat_id)
            return None
        except Exception:
            logger.exception("send_message_safe: fallback failed")
            return None
    except Exception:
        logger.exception("send_message_safe: failed")
        return None

async def delete_message_safe(bot, chat_id: int, message_id: Optional[int]) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramForbiddenError:
        logger.info("delete_message_safe: Forbidden chat=%s — marking blocked", chat_id)
    except TelegramBadRequest:
        pass
    except Exception:
        logger.exception("delete_message_safe: failed")

async def send_video_safe(
    bot,
    chat_id: int,
    video,
    *,
    caption: Optional[str] = None,
    parse_mode: Optional[str] = None,
    reply_markup=None,
    **kwargs,
) -> Optional["Message"]:
    try:
        kw = _strip_duplicates({"parse_mode": parse_mode, "reply_markup": reply_markup}, kwargs)
        return await bot.send_video(
            chat_id,
            video,
            caption=caption,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            **kw,
        )
    except TelegramForbiddenError:
        logger.info("send_video_safe: Forbidden chat=%s — marking blocked", chat_id)
        return None
    except TelegramBadRequest as e:
        if "reply" in str(e).lower() or "message to be replied" in str(e).lower():
            kw = _strip_duplicates({"parse_mode": parse_mode, "reply_markup": reply_markup}, kwargs)
            kw.pop("reply_to_message_id", None)
            try:
                return await bot.send_video(
                    chat_id, video, caption=caption,
                    parse_mode=parse_mode, reply_markup=reply_markup, **kw
                )
            except TelegramForbiddenError:
                logger.info("send_video_safe(no-reply): Forbidden chat=%s", chat_id)
                return None
            except Exception:
                logger.exception("send_video_safe: fallback(no-reply) failed")
                return None
        try:
            kw = _strip_duplicates({"reply_markup": reply_markup}, kwargs)
            return await bot.send_video(
                chat_id, video, caption=caption, reply_markup=reply_markup, **kw
            )
        except TelegramForbiddenError:
            logger.info("send_video_safe (fallback): Forbidden chat=%s", chat_id)
            return None
        except Exception:
            logger.exception("send_video_safe: fallback failed")
            return None
    except Exception:
        logger.exception("send_video_safe: failed")
        return None

async def send_invoice_safe(
    bot,
    *,
    chat_id: int,
    provider_token: str,
    title: str,
    description: str,
    payload: str,
    currency: str,
    prices,
    **kwargs,
):
    try:
        return await bot.send_invoice(
            chat_id=chat_id,
            provider_token=provider_token,
            title=title,
            description=description,
            payload=payload,
            currency=currency,
            prices=prices,
            **kwargs,
        )
    except TelegramForbiddenError:
        logger.info("send_invoice_safe: Forbidden chat=%s — marking blocked", chat_id)
        return None
    except TelegramBadRequest as e:
        logger.warning("send_invoice_safe: BadRequest chat=%s error=%s", chat_id, e)
        return None
    except Exception:
        logger.exception("send_invoice_safe: failed")
        return None