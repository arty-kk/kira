#app/bot/utils/telegram_safe.py
import logging
from dataclasses import dataclass

from typing import Optional, Any, Dict

from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.types import Message


logger = logging.getLogger(__name__)


def _strip_duplicates(explicit: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k not in explicit}

@dataclass(frozen=True)
class SendMessageSafeResult:
    message: Optional["Message"]
    error_code: Optional[str] = None


def _is_closed_loop_error(exc: Exception) -> bool:
    if not isinstance(exc, RuntimeError):
        return False
    msg = str(exc).lower()
    return "event loop is closed" in msg or "timeout context manager should be used inside a task" in msg


def _classify_send_message_error(exc: Exception) -> str:
    if _is_closed_loop_error(exc):
        return "loop_closed"
    return "unknown_send_error"


async def send_message_safe_with_reason(
    bot,
    chat_id: int,
    text: str,
    *,
    parse_mode: Optional[str] = None,
    reply_markup=None,
    **kwargs,
) -> SendMessageSafeResult:
    try:
        kw = _strip_duplicates({"parse_mode": parse_mode, "reply_markup": reply_markup}, kwargs)
        message = await bot.send_message(
            chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup, **kw
        )
        return SendMessageSafeResult(message=message)
    except TelegramForbiddenError:
        logger.info("send_message_safe: Forbidden chat=%s — marking blocked", chat_id)
        return SendMessageSafeResult(message=None, error_code="forbidden")
    except TelegramBadRequest as e:
        if "reply" in str(e).lower() or "message to be replied" in str(e).lower():
            kw = _strip_duplicates({"parse_mode": parse_mode, "reply_markup": reply_markup}, kwargs)
            kw.pop("reply_to_message_id", None)
            try:
                message = await bot.send_message(
                    chat_id, text, parse_mode=parse_mode,
                    reply_markup=reply_markup, **kw
                )
                return SendMessageSafeResult(message=message)
            except TelegramForbiddenError:
                logger.info("send_message_safe(no-reply): Forbidden chat=%s", chat_id)
                return SendMessageSafeResult(message=None, error_code="forbidden")
            except TelegramBadRequest:
                try:
                    message = await bot.send_message(
                        chat_id, text, reply_markup=reply_markup, **kw
                    )
                    return SendMessageSafeResult(message=message)
                except TelegramForbiddenError:
                    logger.info("send_message_safe(plain,no-reply): Forbidden chat=%s", chat_id)
                    return SendMessageSafeResult(message=None, error_code="forbidden")
                except Exception as inner_exc:
                    if _is_closed_loop_error(inner_exc):
                        logger.warning("send_message_safe: цикл событий уже закрыт, отправка пропущена")
                    else:
                        logger.exception("send_message_safe: fallback(no-reply) failed")
                    return SendMessageSafeResult(message=None, error_code=_classify_send_message_error(inner_exc))
            except Exception as inner_exc:
                if _is_closed_loop_error(inner_exc):
                    logger.warning("send_message_safe: цикл событий уже закрыт, отправка пропущена")
                else:
                    logger.exception("send_message_safe: fallback(no-reply) failed")
                return SendMessageSafeResult(message=None, error_code=_classify_send_message_error(inner_exc))
        try:
            kw = _strip_duplicates({"reply_markup": reply_markup}, kwargs)
            message = await bot.send_message(
                chat_id, text, reply_markup=reply_markup, **kw
            )
            return SendMessageSafeResult(message=message)
        except TelegramForbiddenError:
            logger.info("send_message_safe (fallback): Forbidden chat=%s", chat_id)
            return SendMessageSafeResult(message=None, error_code="forbidden")
        except Exception as inner_exc:
            if _is_closed_loop_error(inner_exc):
                logger.warning("send_message_safe: цикл событий уже закрыт, отправка пропущена")
            else:
                logger.exception("send_message_safe: fallback failed")
            return SendMessageSafeResult(message=None, error_code=_classify_send_message_error(inner_exc))
    except Exception as exc:
        if _is_closed_loop_error(exc):
            logger.warning("send_message_safe: цикл событий уже закрыт, отправка пропущена")
        else:
            logger.exception("send_message_safe: failed")
        return SendMessageSafeResult(message=None, error_code=_classify_send_message_error(exc))


async def send_message_safe(
    bot,
    chat_id: int,
    text: str,
    *,
    parse_mode: Optional[str] = None,
    reply_markup=None,
    **kwargs,
) -> Optional["Message"]:
    result = await send_message_safe_with_reason(
        bot,
        chat_id,
        text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
        **kwargs,
    )
    return result.message

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
    def _is_closed_loop_error(exc: Exception) -> bool:
        return isinstance(exc, RuntimeError) and "event loop is closed" in str(exc).lower()

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
            except Exception as e:
                if _is_closed_loop_error(e):
                    logger.warning("send_video_safe: цикл событий уже закрыт, отправка пропущена")
                    return None
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
        except Exception as e:
            if _is_closed_loop_error(e):
                logger.warning("send_video_safe: цикл событий уже закрыт, отправка пропущена")
                return None
            logger.exception("send_video_safe: fallback failed")
            return None
    except Exception as e:
        if _is_closed_loop_error(e):
            logger.warning("send_video_safe: цикл событий уже закрыт, отправка пропущена")
            return None
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
    def _is_closed_loop_error(exc: Exception) -> bool:
        return isinstance(exc, RuntimeError) and "event loop is closed" in str(exc).lower()

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
    except Exception as e:
        if _is_closed_loop_error(e):
            logger.warning("send_invoice_safe: цикл событий уже закрыт, отправка пропущена")
            return None
        logger.exception("send_invoice_safe: failed")
        return None
