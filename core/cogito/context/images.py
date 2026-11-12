"""What an attached image is, read from its own header: format and size in pixels.

The formats are the ones Claude's API takes (JPEG, PNG, GIF, WebP; Anthropic's vision
documentation, read 2026-10-17) and llama.cpp's server decodes through libmtmd. Nothing
here decodes pixels or needs an image library: the header says the format and the
dimensions, which is all the harness needs to refuse what no model accepts and to
estimate what the image costs in the context window.

Token estimate: Claude sees an image as 28x28-pixel patches, ``ceil(w/28) * ceil(h/28)``,
after scaling it down to fit the model's limit (1568 px long edge and 1568 patches on most
models; 2576 px and 4784 on Claude 4.7 and later). Their numbers, not measured here. A local
vision model's projector costs a different, model-specific amount; the same estimate is
used for it, which keeps the window accounting on the side of too much rather than too
little for the models measured so far.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

#: Media types by format, as the providers name them.
MEDIA_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}

#: The largest image Claude's API takes is 8000 px a side (Anthropic, 2026-10-17).
MAX_SIDE = 8000
#: 10 MB once base64-encoded on Claude's API (Anthropic, 2026-10-17): 7.5 MB of bytes.
MAX_BYTES = 7_500_000

PATCH = 28
STANDARD_LONG_EDGE = 1568
STANDARD_MAX_PATCHES = 1568


@dataclass(frozen=True, slots=True)
class ImageInfo:
    format: str
    width: int
    height: int

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES[self.format]


def sniff(head: bytes) -> ImageInfo | None:
    """The format and size of an image from its first bytes (64 KB is plenty for every
    format here except a JPEG with large metadata before its frame header, which needs the
    whole file), or None when it is not one of the four formats or the header is cut."""
    try:
        if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
            width, height = struct.unpack(">II", head[16:24])
            return _info("png", width, height)
        if head[:6] in (b"GIF87a", b"GIF89a"):
            width, height = struct.unpack("<HH", head[6:10])
            return _info("gif", width, height)
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return _webp(head)
        if head[:2] == b"\xff\xd8":
            return _jpeg(head)
    except (struct.error, IndexError):
        return None
    return None


def _info(fmt: str, width: int, height: int) -> ImageInfo | None:
    if width <= 0 or height <= 0:
        return None
    return ImageInfo(fmt, width, height)


def _webp(head: bytes) -> ImageInfo | None:
    chunk = head[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(head[24:27], "little")
        height = 1 + int.from_bytes(head[27:30], "little")
        return _info("webp", width, height)
    if chunk == b"VP8L":
        bits = int.from_bytes(head[21:25], "little")
        return _info("webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8 ":
        width, height = struct.unpack("<HH", head[26:30])
        return _info("webp", width & 0x3FFF, height & 0x3FFF)
    return None


#: JPEG start-of-frame markers (every SOFn except DHT, JPG and DAC).
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg(data: bytes) -> ImageInfo | None:
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 1 if marker == 0xFF else 2
            continue
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker in _SOF:
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return _info("jpeg", width, height)
        i += 2 + length
    return None


def estimated_tokens(info: ImageInfo) -> int:
    """Context-window tokens the image is expected to take, by Claude's standard rule:
    scaled down to fit 1568 px on the long edge and 1568 patches, then one token per
    28x28 patch."""
    scale = min(1.0, STANDARD_LONG_EDGE / max(info.width, info.height))
    width, height = info.width * scale, info.height * scale
    patches = math.ceil(width / PATCH) * math.ceil(height / PATCH)
    if patches > STANDARD_MAX_PATCHES:
        shrink = math.sqrt(STANDARD_MAX_PATCHES / patches)
        patches = math.ceil(width * shrink / PATCH) * math.ceil(height * shrink / PATCH)
    return max(1, min(patches, STANDARD_MAX_PATCHES))
