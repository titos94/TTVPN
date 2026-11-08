"""Service logo and favicon, uploaded from the billing admin.

The operator drops in a single picture; it becomes the logo in the client
cabinet header and the browser tab icon. Files live next to ``data.json`` so
they survive container rebuilds, and nothing about the original panel's
appearance settings is touched.

A square copy is generated with Pillow for the favicon: a wide logo scaled to
32x32 turns into an unreadable smear, so the image is centred on a square
canvas instead (padded, never cropped).
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

MAX_BYTES = 4 * 1024 * 1024

MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "svg": "image/svg+xml",
}

RASTER = ("png", "jpg", "jpeg", "webp", "gif")

# A round 38 px chip only ever needs a couple of hundred pixels; the full-size
# photo is for the desktop sidebar.
OPERATOR_SMALL = "operator_small.webp"
LOGO_MAX_SIDE = 512
OPERATOR_MAX_SIDE = 1024

_SVG_START = re.compile(rb"^\s*(<\?xml[^>]*>\s*)?(<!--.*?-->\s*)*<svg[\s>]", re.S)


def _panel():
    import app as panel
    return panel


def _brand_dir() -> str:
    data_file = str(_panel().DATA_FILE)
    directory = os.path.join(os.path.dirname(data_file) or ".", "branding")
    os.makedirs(directory, exist_ok=True)
    return directory


def _find(directory: str, stem: str) -> str | None:
    for ext in MEDIA_TYPES:
        path = os.path.join(directory, f"{stem}.{ext}")
        if os.path.isfile(path):
            return path
    return None


def _extension(filename: str, payload: bytes) -> str:
    name = str(filename or "").lower().rsplit(".", 1)[-1]
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "webp"
    if payload[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if _SVG_START.match(payload):
        return "svg"
    if name in MEDIA_TYPES:
        return name
    raise ValueError("Поддерживаются PNG, JPEG, WebP, GIF и SVG")


def _square_icon(payload: bytes) -> bytes:
    """A padded square PNG, so the tab icon is not a squashed rectangle."""
    from PIL import Image

    with Image.open(io.BytesIO(payload)) as source:
        image = source.convert("RGBA")
        side = max(image.size)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
        canvas.thumbnail((256, 256), Image.LANCZOS)
        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


def _fit(payload: bytes, max_side: int, quality: int = 86) -> tuple[bytes, str]:
    """Downscale a raster image and re-encode it as WebP (alpha is kept).

    The cabinet is opened on mobile data as often as on fibre, and a 1.5 MB
    photo in a 38 px chip is wasted traffic.
    """
    from PIL import Image

    with Image.open(io.BytesIO(payload)) as source:
        image = source.convert("RGBA")
        if max(image.size) > max_side:
            image.thumbnail((max_side, max_side), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=quality, method=6)
        return buffer.getvalue(), "webp"


def _cover_square(payload: bytes, side: int = 192, top_bias: float = 0.12) -> bytes:
    """A small square crop for the round support chip, biased to the face."""
    from PIL import Image

    with Image.open(io.BytesIO(payload)) as source:
        image = source.convert("RGBA")
        width, height = image.size
        edge = min(width, height)
        left = (width - edge) // 2
        top = max(0, int((height - edge) * top_bias))
        image = image.crop((left, top, left + edge, top + edge))
        image.thumbnail((side, side), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=88, method=6)
        return buffer.getvalue()


def _prepare(payload: bytes, ext: str, max_side: int) -> tuple[bytes, str]:
    """Normalise an upload: raster files are shrunk and converted to WebP."""
    if ext in RASTER and ext != "gif":
        try:
            return _fit(payload, max_side)
        except Exception:  # noqa: BLE001 - keep the original rather than fail
            logger.exception("branding: could not optimise the image")
    return payload, ext


def current() -> dict:
    """What templates need: urls (cache-busted) and a boolean."""
    empty = {"ready": False, "logo_url": "", "icon_url": "", "version": 0,
             "operator_ready": False, "operator_url": "", "operator_small_url": ""}
    try:
        directory = _brand_dir()
    except Exception:  # noqa: BLE001 - branding must never break a page
        return empty
    logo = _find(directory, "logo")
    operator = _find(directory, "operator")
    operator_url = ""
    operator_small_url = ""
    if operator:
        stamp = int(os.path.getmtime(operator))
        operator_url = f"/billing/brand/operator?v={stamp}"
        # The endpoint generates and caches the square crop on demand; pointing
        # at it even before the file exists is what makes that happen.
        operator_small_url = f"/billing/brand/operator-small?v={stamp}"
    if not logo:
        return {**empty, "operator_ready": bool(operator),
                "operator_url": operator_url,
                "operator_small_url": operator_small_url or operator_url}
    version = int(os.path.getmtime(logo))
    return {
        "ready": True,
        "logo_url": f"/billing/brand/logo?v={version}",
        "icon_url": f"/billing/brand/icon?v={version}",
        "version": version,
        "operator_ready": bool(operator),
        "operator_url": operator_url,
        "operator_small_url": operator_small_url or operator_url,
    }


def logo_file() -> tuple[bytes, str] | None:
    try:
        path = _find(_brand_dir(), "logo")
    except Exception:  # noqa: BLE001
        return None
    if not path:
        return None
    with open(path, "rb") as handle:
        return handle.read(), MEDIA_TYPES.get(path.rsplit(".", 1)[-1], "application/octet-stream")


def operator_file() -> tuple[bytes, str] | None:
    try:
        directory = _brand_dir()
    except Exception:  # noqa: BLE001
        return None
    path = _find(directory, "operator")
    if not path:
        return None
    with open(path, "rb") as handle:
        return handle.read(), MEDIA_TYPES.get(path.rsplit(".", 1)[-1], "application/octet-stream")


def operator_small_file() -> tuple[bytes, str] | None:
    """The square avatar used by the phone-sized support chip.

    Built lazily from the full picture: deployments that uploaded the operator
    photo before this existed still get a ~10 KB WebP instead of shipping a
    1.5 MB photo into a 38 px circle. ``None`` means "not possible" (svg/gif),
    and the caller falls back to the original file.
    """
    try:
        directory = _brand_dir()
    except Exception:  # noqa: BLE001
        return None
    cached = os.path.join(directory, OPERATOR_SMALL)
    if os.path.isfile(cached):
        with open(cached, "rb") as handle:
            return handle.read(), "image/webp"
    path = _find(directory, "operator")
    if not path:
        return None
    ext = path.rsplit(".", 1)[-1].lower()
    if ext not in RASTER or ext == "gif":
        return None
    try:
        with open(path, "rb") as handle:
            small = _cover_square(handle.read())
    except Exception:  # noqa: BLE001 - a chip without a crop is better than none
        logger.exception("branding: could not build the small operator avatar")
        return None
    try:
        with open(cached, "wb") as handle:
            handle.write(small)
    except OSError:
        logger.warning("branding: could not cache %s", OPERATOR_SMALL)
    return small, "image/webp"


def icon_file() -> tuple[bytes, str] | None:
    try:
        directory = _brand_dir()
    except Exception:  # noqa: BLE001
        return None
    path = _find(directory, "icon") or _find(directory, "logo")
    if not path:
        return None
    with open(path, "rb") as handle:
        return handle.read(), MEDIA_TYPES.get(path.rsplit(".", 1)[-1], "application/octet-stream")


def save(filename: str, raw: bytes) -> dict:
    """Store the uploaded picture and rebuild the square favicon."""
    if not raw:
        raise ValueError("Файл пустой")
    if len(raw) > MAX_BYTES:
        raise ValueError("Файл больше 4 МБ — уменьшите картинку")

    ext = _extension(filename, raw)
    directory = _brand_dir()
    payload, ext = _prepare(raw, ext, LOGO_MAX_SIDE)
    for existing in (os.listdir(directory) if os.path.isdir(directory) else []):
        if existing.startswith(("logo.", "icon.")):
            try:
                os.remove(os.path.join(directory, existing))
            except OSError:
                logger.warning("branding: could not remove %s", existing)

    with open(os.path.join(directory, f"logo.{ext}"), "wb") as handle:
        handle.write(payload)

    if ext in RASTER and ext != "gif":
        try:
            icon = _square_icon(payload)
            with open(os.path.join(directory, "icon.png"), "wb") as handle:
                handle.write(icon)
        except Exception:  # noqa: BLE001 - logo alone is still usable
            logger.exception("branding: could not build the square icon")
    return current()


def decode_upload(payload: str) -> bytes:
    """Accept a data URL or plain base64 from the admin page."""
    text = str(payload or "").strip()
    if not text:
        raise ValueError("Файл не выбран")
    if text.startswith("data:"):
        _, _, text = text.partition(",")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Не удалось прочитать файл") from None


def save_operator(filename: str, raw: bytes) -> dict:
    if not raw:
        raise ValueError("Файл пустой")
    if len(raw) > MAX_BYTES:
        raise ValueError("Файл больше 4 МБ — уменьшите картинку")
    ext = _extension(filename, raw)
    directory = _brand_dir()
    payload, ext = _prepare(raw, ext, OPERATOR_MAX_SIDE)
    for existing in (os.listdir(directory) if os.path.isdir(directory) else []):
        if existing.startswith("operator."):
            try:
                os.remove(os.path.join(directory, existing))
            except OSError:
                logger.warning("branding: could not remove %s", existing)
    try:
        os.remove(os.path.join(directory, OPERATOR_SMALL))
    except OSError:
        pass
    with open(os.path.join(directory, f"operator.{ext}"), "wb") as handle:
        handle.write(payload)
    if ext in RASTER and ext != "gif":
        try:
            small = _cover_square(payload)
            with open(os.path.join(directory, OPERATOR_SMALL), "wb") as handle:
                handle.write(small)
        except Exception:  # noqa: BLE001 - the CSS crop still works
            logger.exception("branding: could not build the small operator avatar")
    return current()


def delete_operator() -> None:
    try:
        directory = _brand_dir()
    except Exception:  # noqa: BLE001
        return
    for name in os.listdir(directory):
        if name.startswith("operator"):
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                logger.warning("branding: could not remove %s", name)


def delete() -> None:
    try:
        directory = _brand_dir()
    except Exception:  # noqa: BLE001
        return
    for name in os.listdir(directory):
        if name.startswith(("logo.", "icon.")):
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                logger.warning("branding: could not remove %s", name)


def uploaded_at() -> float:
    try:
        path = _find(_brand_dir(), "logo")
    except Exception:  # noqa: BLE001
        return 0.0
    return os.path.getmtime(path) if path else 0.0


def age_seconds() -> float:
    stamp = uploaded_at()
    return (time.time() - stamp) if stamp else 0.0
