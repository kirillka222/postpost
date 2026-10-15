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
MAX_BIG_BYTES = 200 * 1024 * 1024  # крупнее аккаунт не качаем: ffmpeg на слабом хостинге не справится
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


CLEAN_DEFAULT: dict = {"pos": "br", "w": 30, "h": 12, "mode": "blur"}
CLEAN_MODES = {"blur": "замазать", "crop": "обрезать"}
CLEAN_W, CLEAN_H, CLEAN_STEP = (5, 60), (3, 40), 5


def normalize_clean(c: Optional[dict]) -> dict:
    r = {**CLEAN_DEFAULT, **(c or {})}
    r["w"] = min(max(int(r["w"]), CLEAN_W[0]), CLEAN_W[1])
    r["h"] = min(max(int(r["h"]), CLEAN_H[0]), CLEAN_H[1])
    if r["pos"] not in POSITIONS:
        r["pos"] = CLEAN_DEFAULT["pos"]
    if r["mode"] not in CLEAN_MODES:
        r["mode"] = "blur"
    return r


def clean_plan(width: int, height: int, clean: dict) -> tuple[str, tuple[int, int, int, int]]:
    """Что сделать с кадром, чтобы убрать чужой знак: ('crop', (x, y, w, h) — что оставить) или ('blur', (x, y, w, h)).

    Обрезать можно только полосу у края (углы и стороны); у знака по центру остаётся замазывание.
    """
    c = normalize_clean(clean)
    _, fx, fy = POSITIONS[c["pos"]]
    w, h = max(int(width * c["w"] / 100), 2), max(int(height * c["h"] / 100), 2)
    if c["mode"] == "crop":
        if fy in (0.0, 1.0) and height - h >= 16:
            return "crop", (0, h if fy == 0.0 else 0, width, height - h)
        if fx in (0.0, 1.0) and width - w >= 16:
            return "crop", (w if fx == 0.0 else 0, 0, width - w, height)
    x, y = int((width - w) * fx), int((height - h) * fy)
    x, y = min(max(x, 1), max(width - 3, 1)), min(max(y, 1), max(height - 3, 1))  # delogo не любит рамку у самого края
    return "blur", (x, y, max(min(w, width - x - 1), 1), max(min(h, height - y - 1), 1))


def _remove_image(img: Any, clean: dict) -> Any:
    from PIL import Image, ImageDraw, ImageFilter

    kind, (x, y, w, h) = clean_plan(img.width, img.height, clean)
    if kind == "crop":
        return img.crop((x, y, x + w, y + h))
    base = img.convert("RGB")
    mask = Image.new("L", base.size, 0)
    ImageDraw.Draw(mask).rectangle((x, y, x + w, y + h), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(min(w, h) // 8, 2)))  # плавный край, без «заплатки»
    blurred = base.filter(ImageFilter.GaussianBlur(max(min(w, h) // 3, 8)))
    return Image.composite(blurred, base, mask)


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


def image_bytes(data: bytes, spec: Optional[dict], remove: Optional[dict] = None) -> bytes:
    """Фото (JPEG): сначала убираем чужой знак (remove), потом накладываем свой (spec); любой из шагов можно пропустить."""
    from PIL import Image, ImageOps

    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    except Exception as exc:  # noqa: BLE001
        raise WatermarkError(f"не удалось открыть фото: {exc}") from exc
    if remove:
        img = _remove_image(img, remove)
    base = img.convert("RGBA")
    if spec is not None:
        base = Image.alpha_composite(base, overlay(base.width, base.height, spec))
    out = base.convert("RGB")
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


async def video_bytes(data: bytes, spec: Optional[dict], filename: str = "video.mp4",
                      remove: Optional[dict] = None) -> bytes:
    """Видео (MP4, H.264, звук копируется): убираем чужой знак (remove) и/или накладываем свой (spec), за один проход."""
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
            chain, label = [], "[0:v]"
            if remove:
                kind, (x, y, cw, ch) = clean_plan(w, h, remove)
                chain.append(f"{label}crop={cw}:{ch}:{x}:{y}[a]" if kind == "crop"
                             else f"{label}delogo=x={x}:y={y}:w={cw}:h={ch}[a]")
                label = "[a]"
                if kind == "crop":
                    w, h = cw, ch
            inputs = ["-i", src]
            if spec is not None:
                await asyncio.to_thread(lambda: overlay(w, h, spec).save(ov, "PNG"))
                inputs += ["-i", ov]
                chain.append(f"{label}[1:v]overlay=0:0:format=auto[b]")
                label = "[b]"
            chain.append(f"{label}scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p[v]")
            base = [ffmpeg, "-v", "error", "-y", *inputs, "-filter_complex", ";".join(chain),
                    "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-movflags", "+faststart"]
            code, err = await _run(*base, "-c:a", "copy", out)
            if code:  # звук в необычном формате: перекодируем
                code, err = await _run(*base, "-c:a", "aac", "-b:a", "128k", out)
            if code or not os.path.exists(out):
                raise WatermarkError(f"ffmpeg не справился ({err.strip()[-120:]})")
            return Path(out).read_bytes()


async def apply_items(items: list[Any], spec: Optional[dict], fetch: Any = None,
                      remove: Optional[dict] = None) -> list[Any]:
    """Накладывает знак (spec) и/или убирает чужой (remove) на фото и видео поста; документы и звук не трогает.

    fetch(item) — скачивание файла аккаунтом: так знак ложится и на файлы больше 20 МБ (лимит бота).

    Если знак нанести нельзя (файл не скачан, нет ffmpeg…), бросает WatermarkError: пост без знака не выходит.
    """
    if spec is not None:
        spec = normalize(spec)
        if not clean_text(spec["text"]):
            raise WatermarkError("текст водяного знака не задан (Настройки → Водяной знак)")
    elif remove is None:
        return list(items)
    if remove is not None:
        remove = normalize_clean(remove)
    out: list[Any] = []
    for item in items:
        if item.kind not in ("photo", "video"):
            out.append(item)
            continue
        big = False
        if getattr(item, "big", False) or not item.data:
            if fetch is None or getattr(item, "source", None) is None:
                raise WatermarkError("файл больше 20 МБ, а аккаунт-читатель не подключён: знак не наложить")
            if getattr(item, "size", 0) > MAX_BIG_BYTES:
                raise WatermarkError(f"файл больше {MAX_BIG_BYTES // 2**20} МБ: знак на такое видео не наложить")
            try:
                item = replace(item, data=await fetch(item), big=False)
            except Exception as exc:  # noqa: BLE001
                raise WatermarkError(f"аккаунт не смог скачать файл: {exc}") from exc
            big = True
        if item.kind == "photo":
            data = await asyncio.to_thread(image_bytes, item.data, spec, remove)
            out.append(replace(item, data=data, source=None, filename="photo.jpg", force_account=big))
        else:
            if item.duration and item.duration > MAX_VIDEO_SECONDS:
                raise WatermarkError(f"видео длиннее {MAX_VIDEO_SECONDS // 60} минут")
            data = await video_bytes(item.data, spec, item.filename, remove)
            out.append(replace(item, data=data, source=None, thumb=None, filename="video.mp4", force_account=big))
    return out


def _sample_base() -> bytes:
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
    return buf.getvalue()


def sample(spec: dict) -> bytes:
    """Пример: знак на тестовом кадре 1280×720 (для кнопки «Показать пример»)."""
    return image_bytes(_sample_base(), spec)


def clean_sample(clean: dict) -> bytes:
    """Пример очистки: на тестовый кадр кладём «чужой знак» в выбранное место и убираем его настройками clean."""
    c = normalize_clean(clean)
    fake = {"text": "@чужой_знак", "pos": c["pos"], "size": 22, "opacity": 100}
    return image_bytes(image_bytes(_sample_base(), fake), None, remove=c)