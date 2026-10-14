"""Водяной знак: текст пользователя превращается в полупрозрачную надпись и накладывается на фото и видео поста.

Картинка накладывается Pillow, видео — ffmpeg (ищем системный, а если его нет — из pip-пакета imageio-ffmpeg).
Знак рисуется один раз на весь кадр (PNG с прозрачностью), поэтому фото и видео обрабатываются одинаково.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

FONT = Path(__file__).parent / "assets" / "DejaVuSans-Bold.ttf"
MAX_TEXT = 60
MAX_VIDEO_SECONDS = 600
FFMPEG_TIMEOUT = 600.0

POSITIONS = {  # код: (подпись, по горизонтали 0..1, по вертикали 0..1)
    "tl": ("↖️", 0.0, 0.0), "tc": ("⬆️", 0.5, 0.0), "tr": ("↗️", 1.0, 0.0),
    "ml": ("⬅️", 0.0, 0.5), "c": ("⏺", 0.5, 0.5), "mr": ("➡️", 1.0, 0.5),
    "bl": ("↙️", 0.0, 1.0), "bc": ("⬇️", 0.5, 1.0), "br": ("↘️", 1.0, 1.0),
}
POSITION_NAMES = {
    "tl": "слева сверху", "tc": "сверху", "tr": "справа сверху", "ml": "слева", "c": "по центру", "mr": "справа",
    "bl": "слева снизу", "bc": "снизу", "br": "справа снизу",
}
SIZE_RANGE, OPACITY_RANGE, STEP = (10, 80), (10, 100), 10
DEFAULT: dict = {"text": "", "pos": "br", "size": 30, "opacity": 60, "feeds": False}

_sem: Optional[asyncio.Semaphore] = None


class WatermarkError(Exception):
    """Знак не удалось нанести (текст ошибки можно показать пользователю)."""


def clean_text(raw: str) -> str:
    """Оставляет то, что шрифт умеет рисовать: буквы, цифры, знаки; без эмодзи и управляющих символов."""
    out = []
    for ch in raw.replace("\r", ""):
        if ch == "\n" or (ord(ch) <= 0xFFFF and unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn", "So")):
            out.append(ch)
    text = re.sub(r"[ \t]+", " ", "".join(out))
    text = "\n".join(line.strip() for line in text.split("\n")[:2]).strip()  # не больше двух строк
    return text[:MAX_TEXT]


def normalize(row: Optional[dict]) -> dict:
    r = {**DEFAULT, **(row or {})}
    r["size"] = min(max(int(r["size"]), SIZE_RANGE[0]), SIZE_RANGE[1])
    r["opacity"] = min(max(int(r["opacity"]), OPACITY_RANGE[0]), OPACITY_RANGE[1])
    if r["pos"] not in POSITIONS:
        r["pos"] = DEFAULT["pos"]
    r["feeds"] = bool(r["feeds"])
    return r


def ready(spec: Optional[dict]) -> bool:
    """Можно ли накладывать знак: он включён (spec не None) и текст не пуст."""
    return spec is not None and bool(clean_text(spec.get("text", "")))


def has_visual(payload: Optional[dict]) -> bool:
    """В снимке поста есть фото или видео, на которые можно наложить знак."""
    return any(m.get("kind") in ("photo", "video") for m in (payload or {}).get("media", []))


def step(value: int, delta: int, bounds: tuple[int, int]) -> int:
    return min(max(value + delta, bounds[0]), bounds[1])


def overlay(width: int, height: int, spec: dict) -> Any:
    """Прозрачный слой размером с кадр с готовой надписью (Pillow RGBA)."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    spec = normalize(spec)
    text = clean_text(spec["text"])
    if not text:
        raise WatermarkError("Текст водяного знака пуст")
    if not FONT.exists():
        raise WatermarkError("Не найден шрифт водяного знака (bot/assets/DejaVuSans-Bold.ttf)")
    target_w = max(int(width * spec["size"] / 100), 24)
    # подбираем размер шрифта так, чтобы самая длинная строка заняла нужную долю ширины
    probe = ImageFont.truetype(str(FONT), 100)
    longest = max(probe.getlength(line) for line in text.split("\n")) or 1
    px = max(int(100 * target_w / longest), 10)
    font = ImageFont.truetype(str(FONT), px)
    pad = max(px // 3, 4)
    spacing = max(px // 6, 2)
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = dummy.multiline_textbbox((0, 0), text, font=font, spacing=spacing, align="center")
    box = tuple(int(round(v)) for v in box)
    tw, th = box[2] - box[0], box[3] - box[1]
    alpha = int(255 * spec["opacity"] / 100)

    layer = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    off = max(px // 18, 1)
    ImageDraw.Draw(shadow).multiline_text((pad - box[0] + off, pad - box[1] + off), text, font=font, spacing=spacing,
                                          align="center", fill=(0, 0, 0, int(alpha * 0.7)))
    layer = Image.alpha_composite(layer, shadow.filter(ImageFilter.GaussianBlur(max(px // 22, 1))))
    ImageDraw.Draw(layer).multiline_text((pad - box[0], pad - box[1]), text, font=font, spacing=spacing,
                                         align="center", fill=(255, 255, 255, alpha))

    _, fx, fy = POSITIONS[spec["pos"]]
    margin = max(int(min(width, height) * 0.03), 6)
    x = int(margin + (width - layer.width - 2 * margin) * fx)
    y = int(margin + (height - layer.height - 2 * margin) * fy)
    full = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    full.paste(layer, (max(x, 0), max(y, 0)))
    return full


def image_bytes(data: bytes, spec: dict) -> bytes:
    """Фото с водяным знаком (JPEG)."""
    from PIL import Image, ImageOps

    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    except Exception as exc:  # noqa: BLE001
        raise WatermarkError(f"не удалось открыть фото: {exc}") from exc
    base = img.convert("RGBA")
    out = Image.alpha_composite(base, overlay(base.width, base.height, spec)).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()


def find_ffmpeg() -> Optional[str]:
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


async def _run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise WatermarkError("видео обрабатывалось слишком долго") from None
    return proc.returncode or 0, err.decode("utf-8", "replace")[-400:]


async def video_bytes(data: bytes, spec: dict, filename: str = "video.mp4") -> bytes:
    """Видео с водяным знаком (MP4, H.264; звук копируется)."""
    global _sem
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise WatermarkError("на сервере нет ffmpeg (добавьте imageio-ffmpeg в requirements.txt)")
    if _sem is None:
        _sem = asyncio.Semaphore(1)  # одно видео за раз: на слабом хостинге ffmpeg съедает весь процессор
    suffix = os.path.splitext(filename)[1] or ".mp4"
    async with _sem:
        with tempfile.TemporaryDirectory(prefix="flypost-wm-") as tmp:
            src, frame, ov, out = (os.path.join(tmp, n) for n in (f"in{suffix}", "frame.png", "wm.png", "out.mp4"))
            Path(src).write_bytes(data)
            # размер кадра берём с настоящего первого кадра: он уже повёрнут так, как видит зритель
            code, err = await _run(ffmpeg, "-v", "error", "-y", "-i", src, "-frames:v", "1", frame)
            if code or not os.path.exists(frame):
                raise WatermarkError(f"не удалось прочитать видео ({err.strip()[-120:] or 'ffmpeg'})")
            from PIL import Image

            with Image.open(frame) as fr:
                w, h = fr.size
            await asyncio.to_thread(lambda: overlay(w, h, spec).save(ov, "PNG"))
            base = [ffmpeg, "-v", "error", "-y", "-i", src, "-i", ov, "-filter_complex",
                    "[0:v][1:v]overlay=0:0:format=auto,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p[v]",
                    "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-movflags", "+faststart"]
            code, err = await _run(*base, "-c:a", "copy", out)
            if code:  # звук в необычном формате: перекодируем
                code, err = await _run(*base, "-c:a", "aac", "-b:a", "128k", out)
            if code or not os.path.exists(out):
                raise WatermarkError(f"ffmpeg не справился ({err.strip()[-120:]})")
            return Path(out).read_bytes()


async def apply_items(items: list[Any], spec: dict) -> list[Any]:
    """Накладывает знак на фото и видео поста; документы и звук не трогает.

    Если знак нанести нельзя (файл не скачан, нет ffmpeg…), бросает WatermarkError: пост без знака не выходит.
    """
    spec = normalize(spec)
    if not clean_text(spec["text"]):
        raise WatermarkError("текст водяного знака не задан (Настройки → Водяной знак)")
    out: list[Any] = []
    for item in items:
        if item.kind not in ("photo", "video"):
            out.append(item)
            continue
        if getattr(item, "big", False) or not item.data:
            raise WatermarkError("файл больше 20 МБ: бот не может наложить на него знак")
        if item.kind == "photo":
            data = await asyncio.to_thread(image_bytes, item.data, spec)
            out.append(replace(item, data=data, source=None, filename="photo.jpg"))
        else:
            if item.duration and item.duration > MAX_VIDEO_SECONDS:
                raise WatermarkError(f"видео длиннее {MAX_VIDEO_SECONDS // 60} минут")
            data = await video_bytes(item.data, spec, item.filename)
            out.append(replace(item, data=data, source=None, thumb=None, filename="video.mp4"))
    return out


def sample(spec: dict) -> bytes:
    """Пример: знак на тестовом кадре 1280×720 (для кнопки «Показать пример»)."""
    from PIL import Image, ImageDraw

    w, h = 1280, 720
    img = Image.new("RGB", (w, h))
    px = ImageDraw.Draw(img)
    for y in range(h):  # плавный сине-розовый градиент
        t = y / h
        px.line([(0, y), (w, y)], fill=(int(40 + 110 * t), int(70 + 40 * t), int(150 - 40 * t)))
    for cx, cy, r, col in ((300, 360, 150, (255, 205, 90)), (860, 300, 110, (120, 220, 190)),
                           (1040, 520, 90, (240, 130, 150))):
        px.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return image_bytes(buf.getvalue(), spec)