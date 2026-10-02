"""Verify and normalise every image the app stores (spec §71.4).

Input is a base64 data URI, as the request models already accept. Output is a
data URI in the profile's format: re-encoded, orientation applied, metadata
dropped, size bounded. The decoder runs under a pixel limit, SVG is refused,
and an animated GIF becomes its first frame.

`process_data_uri` is synchronous and CPU-bound. Routers call
`process_data_uri_async`, which runs it in the thread pool so the single
uvicorn worker keeps serving and the delivery tick is not stalled.
"""
import base64
import binascii
import io
import re
from dataclasses import dataclass

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_ENCODED_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 40_000_000
MIN_QUALITY = 40
ACCEPTED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}
_DATA_URI_RE = re.compile(r"^data:([a-zA-Z0-9.+/-]+);base64,([A-Za-z0-9+/\s]+=*)$")
_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}

# Set once at import: Pillow warns at this many pixels and raises at twice it.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ImageRejected(ValueError):
    """The image cannot be stored; the message says why."""


@dataclass(frozen=True)
class ImageProfile:
    name: str
    max_width: int
    max_height: int
    output_format: str          # "JPEG", "PNG" or "WEBP"
    quality: int = 85
    square: bool = False        # center-crop to a square before bounding
    keep_alpha: bool = False    # False: flatten onto white
    crop: tuple[int, int] | None = None      # fill and center-crop to exactly this size
    min_size: tuple[int, int] | None = None  # refuse smaller sources (never upscale)
    max_bytes: int | None = None             # lower the quality until the output fits


EVENT_COVER = ImageProfile("event_cover", 1600, 800, "JPEG", quality=85)
TENANT_ICON = ImageProfile("tenant_icon", 256, 256, "PNG", square=True, keep_alpha=True)
# Theme art (spec §71.15): exact sizes, so a theme's layout never depends on an upload's shape.
THEME_BANNER = ImageProfile("theme_banner", 1200, 360, "WEBP", quality=82, crop=(1200, 360), min_size=(1200, 360), max_bytes=100_000)
THEME_HEADER = ImageProfile("theme_header", 1920, 400, "WEBP", quality=80, crop=(1920, 400), min_size=(1920, 400), max_bytes=150_000)
THEME_HEADER_MOBILE = ImageProfile("theme_header_mobile", 900, 500, "WEBP", quality=80, crop=(900, 500), min_size=(900, 500), max_bytes=80_000)


def _decode(data_uri: str) -> tuple[Image.Image, bytes]:
    if len(data_uri) > MAX_ENCODED_BYTES:
        raise ImageRejected("image is larger than 8 MB")
    match = _DATA_URI_RE.match(data_uri)
    if not match:
        raise ImageRejected("must be a PNG, JPEG, WebP or GIF image")
    if match.group(1).lower() == "image/svg+xml":
        raise ImageRejected("SVG images are not accepted")
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
    except (binascii.Error, ValueError):
        raise ImageRejected("is not valid base64") from None
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Image.DecompressionBombError:
        raise ImageRejected("has too many pixels (limit 40 million)") from None
    except (UnidentifiedImageError, OSError, SyntaxError):
        raise ImageRejected("could not be read as an image") from None
    if image.format not in ACCEPTED_FORMATS:
        raise ImageRejected("must be a PNG, JPEG, WebP or GIF image")
    return image, raw


def _is_already_normal(image: Image.Image, raw: bytes, profile: ImageProfile) -> bool:
    """True when re-encoding would only lose quality: same format, in bounds,
    no EXIF, no animation, and (for a square profile) already square."""
    if image.format != profile.output_format or getattr(image, "n_frames", 1) > 1:
        return False
    if image.width > profile.max_width or image.height > profile.max_height:
        return False
    if profile.square and image.width != image.height:
        return False
    if not profile.keep_alpha and image.mode in ("RGBA", "LA", "P"):
        return False
    return not image.getexif() and b"Exif" not in raw[:4096]


def _encode(image: Image.Image, profile: ImageProfile) -> bytes:
    out = io.BytesIO()
    if profile.output_format == "JPEG":
        image.convert("RGB").save(out, "JPEG", quality=profile.quality, optimize=True)
    elif profile.output_format == "WEBP":
        quality = profile.quality
        while True:
            out = io.BytesIO()
            image.save(out, "WEBP", quality=quality)
            if profile.max_bytes is None or out.tell() <= profile.max_bytes:
                break
            if quality <= MIN_QUALITY:
                raise ImageRejected(f"is too detailed to fit {profile.max_bytes // 1000} KB; use a simpler or softer image")
            quality -= 8
    else:
        image.save(out, "PNG", optimize=True)
    return out.getvalue()


def process_data_uri(data_uri: str, profile: ImageProfile) -> str:
    """Return a normalised data URI for `profile`, or raise ImageRejected."""
    image, raw = _decode(data_uri)
    if profile.min_size and (image.width < profile.min_size[0] or image.height < profile.min_size[1]):
        raise ImageRejected(f"is too small: at least {profile.min_size[0]} by {profile.min_size[1]} pixels")
    if not profile.crop and _is_already_normal(image, raw, profile):
        return f"data:{_MIME[profile.output_format]};base64,{base64.b64encode(raw).decode()}"
    image.seek(0)
    image = ImageOps.exif_transpose(image)
    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    image = image.convert("RGBA" if has_alpha else "RGB")
    if has_alpha and not profile.keep_alpha:
        flat = Image.new("RGB", image.size, (255, 255, 255))
        flat.paste(image, mask=image.getchannel("A"))
        image = flat
    if profile.square:
        image = ImageOps.fit(image, (min(image.width, image.height),) * 2, Image.Resampling.LANCZOS)
    if profile.crop:
        image = ImageOps.fit(image, profile.crop, Image.Resampling.LANCZOS)
    else:
        image.thumbnail((profile.max_width, profile.max_height), Image.Resampling.LANCZOS)
    encoded = _encode(image, profile)
    return f"data:{_MIME[profile.output_format]};base64,{base64.b64encode(encoded).decode()}"


async def process_data_uri_async(data_uri: str, profile: ImageProfile, label: str) -> str:
    """Thread-pool wrapper for routers; a rejection becomes a 422 naming `label`."""
    try:
        return await run_in_threadpool(process_data_uri, data_uri, profile)
    except ImageRejected as exc:
        raise HTTPException(status_code=422, detail=f"{label} {exc}") from None
