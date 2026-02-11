from __future__ import annotations

import contextlib
import io
import logging
import os
import tempfile
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from app.clients.telegram_client import get_bot
from app.config import settings

logger = logging.getLogger(__name__)

try:
    RESAMPLING = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLING = Image.LANCZOS

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_SIDE = 2048
ALLOWED_FORMATS = {"JPEG", "JPG", "PNG", "WEBP"}
MAX_FRAMES = 1
MIN_JPEG_QUALITY = int(getattr(settings, "MIN_JPEG_QUALITY", 35))
MIN_SIDE = int(getattr(settings, "MIN_IMAGE_SIDE", 720))

Image.MAX_IMAGE_PIXELS = int(getattr(settings, "MAX_IMAGE_PIXELS", 36_000_000))


async def download_to_tmp(tg_obj: Any, suffix: str) -> str | None:
    tmp_path: str | None = None
    try:
        bot = get_bot()
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        await bot.download(tg_obj, tmp_path)
        return tmp_path
    except Exception:
        logger.exception("Failed to download file")
        if tmp_path and os.path.exists(tmp_path):
            with contextlib.suppress(Exception):
                os.remove(tmp_path)
        return None


async def strict_image_load(tmp_path: str) -> Image.Image:
    try:
        with Image.open(tmp_path) as im:
            fmt = (im.format or "").upper()
            if fmt == "JPG":
                fmt = "JPEG"
            if fmt not in ALLOWED_FORMATS:
                raise ValueError(f"Unsupported image format: {fmt}")
            im.verify()

        with Image.open(tmp_path) as im2:
            im2.load()
            with contextlib.suppress(Exception):
                im2 = ImageOps.exif_transpose(im2)
            return im2.copy()

    except UnidentifiedImageError:
        raise ValueError("Not an image or corrupted file")
    except Image.DecompressionBombError:
        raise ValueError("Image too large (decompression bomb)")
    except Exception as e:
        raise ValueError(str(e))


def sanitize_and_compress(img: Image.Image, *, max_image_bytes: int = MAX_IMAGE_BYTES) -> bytes:
    n_frames = int(getattr(img, "n_frames", 1) or 1)
    if n_frames > MAX_FRAMES:
        raise ValueError("Animated or multi-frame images are not allowed")

    with contextlib.suppress(Exception):
        img = ImageOps.exif_transpose(img)

    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    if max(w, h) > MAX_SIDE:
        s = MAX_SIDE / float(max(w, h))
        img = img.resize((int(w * s), int(h * s)), resample=RESAMPLING)

    def _save_as_jpeg(jimg: Image.Image, q: int) -> bytes:
        buf = io.BytesIO()
        for progressive in (True, False):
            try:
                buf.seek(0)
                buf.truncate(0)
                jimg.save(
                    buf,
                    format="JPEG",
                    quality=q,
                    optimize=True,
                    progressive=progressive,
                    subsampling=2,
                    exif=b"",
                )
                return buf.getvalue()
            except OSError:
                continue

        buf.seek(0)
        buf.truncate(0)
        jimg.save(buf, format="JPEG", quality=q, progressive=False, subsampling=2, exif=b"")
        return buf.getvalue()

    quality_steps = [85, 80, 75, 70, 65, 60, 55, 50, 45, 40, MIN_JPEG_QUALITY]
    for _ in range(6):
        for q in quality_steps:
            data = _save_as_jpeg(img, q)
            if len(data) <= max_image_bytes:
                return data

        cur_max = max(img.size)
        if cur_max <= MIN_SIDE:
            break

        new_max = max(MIN_SIDE, int(cur_max * 0.85))
        s = new_max / float(cur_max)
        img = img.resize(
            (max(1, int(img.size[0] * s)), max(1, int(img.size[1] * s))),
            resample=RESAMPLING,
        )

    img = img.resize((max(1, img.size[0] // 2), max(1, img.size[1] // 2)), resample=RESAMPLING)
    data = _save_as_jpeg(img, max(60, MIN_JPEG_QUALITY))
    if len(data) > max_image_bytes:
        raise ValueError("Image too large after compression")
    return data
