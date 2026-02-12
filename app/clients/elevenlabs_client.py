#app/clients/elevenlabs_client.py
from __future__ import annotations

import json
import aiohttp
import logging
import os

from dataclasses import dataclass, field
from typing import Optional, Dict, Any

from app.config import settings
from app.bot.components.constants import redis_client

logger = logging.getLogger(__name__)

_EL_CLIENT: ElevenLabsClient | None = None
_ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1"
_VOICE_FALLBACK_MAP: Dict[str, str] = {
    "uk": "ru",
    "be": "ru",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-hant": "zh",
}

async def get_elevenlabs_client() -> ElevenLabsClient:
    global _EL_CLIENT
    if _EL_CLIENT is None:
        _EL_CLIENT = ElevenLabsClient.from_settings()
    return _EL_CLIENT

async def choose_elevenlabs_voice(
    user_id: int,
    lang: str,
    override_voice_id: Optional[str],
) -> tuple[ElevenLabsClient, Optional[str]]:
    client = await get_elevenlabs_client()
    voice_id = await client.pick_voice(user_id=user_id, lang=lang, override_voice_id=override_voice_id)

    if voice_id:
        return client, voice_id

    lang_l = (lang or "").lower()
    fb_lang = _VOICE_FALLBACK_MAP.get(lang_l)
    if fb_lang and fb_lang != lang_l:
        try:
            voice_id = await client.pick_voice(user_id=user_id, lang=fb_lang, override_voice_id=override_voice_id)
        except Exception:
            voice_id = None

    if not voice_id:
        try:
            voice_id = await client.pick_voice(user_id=user_id, lang="multi", override_voice_id=override_voice_id)
        except Exception:
            voice_id = None

    if not voice_id and lang_l != "en":
        try:
            voice_id = await client.pick_voice(user_id=user_id, lang="en", override_voice_id=override_voice_id)
        except Exception:
            voice_id = None

    return client, voice_id

async def shutdown_elevenlabs_client() -> None:
    global _EL_CLIENT
    try:
        if _EL_CLIENT is not None:
            await _EL_CLIENT.close()
    finally:
        _EL_CLIENT = None

def _load_voice_map() -> Dict[str, str]:

    raw = getattr(settings, "ELEVENLABS_VOICE_MAP", os.environ.get("ELEVENLABS_VOICE_MAP", "")) or ""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return {str(k).strip().lower(): str(v).strip() for k, v in (data or {}).items() if str(v).strip()}
    except Exception:
        logger.warning("ELEVENLABS_VOICE_MAP is not valid JSON; ignoring")
        return {}

@dataclass
class ElevenLabsClient:
    api_key: str
    default_model_id: str = field(default_factory=lambda: getattr(
        settings, "ELEVENLABS_MODEL_ID", os.environ.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
    ))
    default_output_format: str = field(default_factory=lambda: getattr(
        settings, "ELEVENLABS_OUTPUT_FORMAT",
        os.environ.get("ELEVENLABS_OUTPUT_FORMAT", "opus_48000")
    ))
    timeout: int = field(default_factory=lambda: int(getattr(
        settings, "ELEVENLABS_TIMEOUT", os.environ.get("ELEVENLABS_TIMEOUT", 20)
    )))
    default_voice_map: Dict[str, str] = field(default_factory=_load_voice_map)
    _session: Optional[aiohttp.ClientSession] = None

    @classmethod
    def from_settings(cls) -> "ElevenLabsClient":
        api_key = getattr(settings, "ELEVENLABS_API_KEY", os.environ.get("ELEVENLABS_API_KEY", "")).strip()
        if not api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set")
        return cls(api_key=api_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        headers = {"xi-api-key": self.api_key}
        self._session = aiohttp.ClientSession(timeout=timeout, headers=headers)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _user_pref_key(user_id: int, lang: str) -> str:
        return f"voice_pref:{user_id}:{(lang or '').lower()}"

    async def set_user_voice_pref(self, user_id: int, lang: str, voice_id: str) -> None:
        try:
            await redis_client.set(self._user_pref_key(user_id, lang), voice_id, ex=90 * 24 * 3600)  # 90 дней
        except Exception:
            logger.exception("Failed to set user voice preference")

    async def get_user_voice_pref(self, user_id: int, lang: str) -> Optional[str]:
        try:
            raw = await redis_client.get(self._user_pref_key(user_id, lang))
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "ignore")
            return (raw or "").strip() or None
        except Exception:
            logger.exception("Failed to get user voice preference")
            return None

    async def pick_voice(
        self,
        *,
        user_id: Optional[int],
        lang: str,
        override_voice_id: Optional[str] = None,
    ) -> Optional[str]:

        if override_voice_id:
            return override_voice_id

        lang_l = (lang or "").lower()
        if user_id is not None:
            pref = await self.get_user_voice_pref(user_id, lang_l)
            if pref:
                return pref

        vid = self.default_voice_map.get(lang_l)
        if vid:
            return vid

        fallback = getattr(settings, "ELEVENLABS_DEFAULT_VOICE_ID", os.environ.get("ELEVENLABS_DEFAULT_VOICE_ID", "")).strip()
        return fallback or None

    async def synthesize(
        self,
        *,
        text: str,
        lang: str,
        voice_id: str,
        model_id: Optional[str] = None,
        voice_settings: Optional[Dict[str, Any]] = None,
        output_format: Optional[str] = None,
        apply_text_normalization: Optional[str] = None,  # 'auto'|'on'|'off'
        seed: Optional[int] = None,
    ) -> bytes:

        if not voice_id:
            raise ValueError("voice_id is required")

        session = await self._get_session()
        url = f"{_ELEVENLABS_API_URL}/text-to-speech/{voice_id}"
        payload = {
            "text": text,
            "model_id": (model_id or self.default_model_id),
            "output_format": (output_format or self.default_output_format),
        }
        if voice_settings:
            payload["voice_settings"] = voice_settings
        if apply_text_normalization:
            payload["apply_text_normalization"] = apply_text_normalization
        if seed is not None:
            payload["seed"] = int(seed)

        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                text_err = await resp.text()
                raise RuntimeError(f"ElevenLabs TTS failed: HTTP {resp.status} — {text_err}")
            return await resp.read()

    async def list_voices(self) -> Dict[str, str]:

        session = await self._get_session()
        url = f"{_ELEVENLABS_API_URL}/voices"
        async with session.get(url) as resp:
            if resp.status != 200:
                text_err = await resp.text()
                raise RuntimeError(f"ElevenLabs voices failed: HTTP {resp.status} — {text_err}")
            data = await resp.json()
            out = {}
            for v in (data.get("voices") or []):
                name = (v.get("name") or "").strip()
                vid = (v.get("voice_id") or "").strip()
                if name and vid:
                    out[name] = vid
            return out
