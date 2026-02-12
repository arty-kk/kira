import base64

from app.config import settings

API_MAX_IMAGE_BYTES = int(getattr(settings, "API_MAX_IMAGE_BYTES", 5 * 1024 * 1024))
API_MAX_VOICE_BYTES = int(getattr(settings, "API_MAX_VOICE_BYTES", 25 * 1024 * 1024))

ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
ALLOWED_VOICE_MIMES = {
    "audio/ogg", "audio/opus", "audio/mpeg", "audio/mp3",
    "audio/wav", "audio/x-wav", "audio/webm",
    "audio/mp4", "audio/m4a", "audio/aac",
}


def clean_base64_payload(b64_value: str) -> str:
    return "".join((b64_value or "").split())


def decode_base64_payload(b64_value: str) -> bytes:
    """Shared base64 cleanup/validation helper for API and worker."""
    cleaned = clean_base64_payload(b64_value)
    try:
        return base64.b64decode(cleaned, validate=True)
    except Exception:
        return b""
