"""Files and text the person attached to a message (design 1n).

The window lets a person drop, paste or pick a file, or paste a long text, and shows it as a
chip on the composer. This module reads what the person chose (never writes it), says why
something cannot be attached, puts what can into the message the model gets, and reads it
back out of a stored message for the thread, a rewind and a fold's seed.

The model gets the content inline, after the person's words, each attachment in a tag with
its name, path, size and where the session keeps a copy::

    Revisa por qué el total sale negativo.

    <attached_file name="src/cart.ts" path="/home/ana/tienda-api/src/cart.ts" lines="40"
    bytes="1234" copy="/home/ana/tienda-api/.cogito/artifacts/<session>/<id>.txt">
    ...the file, exactly as it was read...
    </attached_file>

(The header is one line, wrapped here.) A paste is ``<attached_text name="pasted text"
...>``. Other projects, read 2026-09-30 at their current source (cogito-lab
experiments/2026-09-30-attachments/RESEARCH.md has the commits and details):

- Inline, as nearly all of them do. Cline appends ``<file_content path="...">`` after the
  user's text (apps/vscode/src/core/mentions/index.ts), OpenCode inlines a synthetic Read
  (packages/opencode/src/session/prompt.ts), Roo Code inlines a ``[read_file for '...']``
  block, Aider re-sends every added file fenced, and Claude Code's docs say an @-mention
  includes the full file. Only Codex sends just the path
  (codex-rs/protocol/src/user_input.rs). A person who attaches a file has already decided
  the model needs it, so the model is not left to fetch it.
- Bounded, never cut silently. Cline refuses a file over 20 MB and truncates a mention at
  400 KB with a notice; OpenCode and Roo stop at 2,000 lines with an offset hint. Here the
  design's 20 MB is the refusal line. Below it the message goes through the same inbound
  gate as the person's words (`context.inbound`): past a quarter of the window it is saved
  whole and the model gets its beginning, its end and how to read the rest.
- Binary: Cline's SDK and OpenCode check the first bytes for a NUL, and OpenCode also for
  more than 30% control bytes. Here the same two rules run on `read`'s 8 KiB sample, after
  the signatures of the images, video, audio and PDFs a person is most likely to drop, so
  the chip can say which it was.
- An image (PNG, JPEG, GIF, WebP: the formats Claude's API and llama.cpp's projector
  path take) is attached as itself, within Anthropic's limits (10 MB base64, 8000 px a
  side; their vision documentation, read 2026-10-17). The text carries its marker
  (`<attached_image ...>`, no bytes) and the message an `ImageBlock` pointing at the
  session's copy. A model that cannot see images gets the marker with a line saying so,
  so it tells the person instead of answering as if nothing was attached (QA 2026-10-17:
  a JPG was dropped silently and the model said it saw no image). Other image formats
  (BMP, TIFF, HEIC, ICO) are still refused as images.
- A document (PDF, Word, Excel, PowerPoint, OpenDocument) is attached as its text, by the
  converter `read` uses for documents (`tools.documents`). The composition root passes it
  in as ``convert`` because this layer sits below `tools`. The block says so
  (``document="PDF, 3 pages, as text"``) with the file's own size. A document the converter
  cannot read (password, scan, damage) is refused with its reason.
- Compaction: Cline keeps only the paths of older attachments, and Claude Code puts a file
  reference after a compact boundary. Here a fold's seed keeps one line per attachment, what
  it was and where the copy is (`seed_text`), never the content, which
  `compact.render_tasks` would otherwise carry into every handover. A resume replays the
  message as sent and never re-reads the file, as Claude Code does since 2.1.277
  (CHANGELOG).
- Words first, as in Cline, so an offloaded message starts with the person's own words,
  which the inbound gate always shows.

Without attachments nothing here is called: the message is the person's words, the same
string, and every request is byte-identical to before. Benchmarks never attach anything.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cogito.context import images

#: Largest attachment, in bytes: design 1n's «hasta 20 MB cada uno», in decimal megabytes
#: as the design writes every size.
MAX_BYTES = 20_000_000

#: How much of a file is checked to tell text from binary. Same as `read`'s sample
#: (`tools.files._NULL_SAMPLE`), so the two never disagree about a file.
SAMPLE_BYTES = 8192

#: Why something cannot be attached, as the window names it on the chip. A code, never a
#: sentence: the window's copy is fixed (strings.js).
FOLDER = "folder"
UNREADABLE = "unreadable"
TOO_LARGE = "too_large"
IMAGE = "image"
#: An image past what a provider accepts (7.5 MB, or 8000 px a side).
IMAGE_TOO_LARGE = "image_too_large"
VIDEO = "video"
AUDIO = "audio"
PDF = "pdf"
BINARY = "binary"
#: A document the converter (`tools.documents`, passed in by the composition root) could
#: not turn into text: it needs a password, it is scanned pages, or it is damaged.
ENCRYPTED = "encrypted"
SCANNED = "scanned"
CORRUPT = "corrupt"
REASONS = (
    FOLDER, UNREADABLE, TOO_LARGE, IMAGE, IMAGE_TOO_LARGE, VIDEO, AUDIO, PDF, BINARY,
    ENCRYPTED, SCANNED, CORRUPT,
)

#: The converter's reasons, mapped to ours.
_DOCUMENT_REASONS = {
    "encrypted": ENCRYPTED,
    "scanned": SCANNED,
    "corrupt": CORRUPT,
    "too_large": TOO_LARGE,
}

#: Turns a document into text, shaped like `tools.documents.extract`: None for a file that
#: is not a document, an object with ``reason`` for one that cannot be read, else one with
#: ``text`` and ``describe()``.
Convert = Callable[[Path], Any]

# File type from the first bytes, for the formats a person is most likely to drop that a
# text model cannot read. Signatures come from each format's specification (PNG, JPEG/JFIF,
# GIF87a/89a, RIFF WEBP/WAVE/AVI, ISO BMFF `ftyp`, Matroska/WebM EBML, ID3, FLAC, Ogg,
# PDF). Anything else with a NUL in the sample is binary, as in `read`.
#: Each is a prefix no text file starts with: it has a byte that is not UTF-8 text, or a NUL
#: in a fixed place (a BMP's reserved field, an ID3 tag's revision, an Ogg page's version).
#: A text file that starts with "BM" or "ID3" is still text.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", IMAGE),
    (b"\xff\xd8\xff", IMAGE),
    (b"GIF87a", IMAGE),
    (b"GIF89a", IMAGE),
    (b"II*\x00", IMAGE),
    (b"MM\x00*", IMAGE),
    (b"\x00\x00\x01\x00", IMAGE),
    (b"%PDF-", PDF),
    (b"\x1aE\xdf\xa3", VIDEO),
    (b"fLaC\x00", AUDIO),
    (b"OggS\x00", AUDIO),
)
_RIFF = {b"WEBP": IMAGE, b"WAVE": AUDIO, b"AVI ": VIDEO}
#: ISO BMFF brands (bytes 8-12, after `ftyp`) that are still images. Every other brand is
#: video, except `M4A `/`M4B `, the audio brands a person drops.
_FTYP_IMAGE = {b"heic", b"heix", b"mif1", b"msf1", b"avif", b"avis"}
_FTYP_AUDIO = {b"M4A ", b"M4B "}


def _signature(head: bytes) -> str | None:
    for magic, kind in _SIGNATURES:
        if head.startswith(magic):
            return kind
    if head[:2] == b"BM" and head[6:10] == b"\x00\x00\x00\x00":
        return IMAGE
    if head[:3] == b"ID3" and head[3:4] in (b"\x02", b"\x03", b"\x04") and head[4:5] == b"\x00":
        return AUDIO
    if head[:4] == b"RIFF" and head[8:12] in _RIFF:
        return _RIFF[head[8:12]]
    # An ISO BMFF file starts with its first box: a small 32-bit size, then `ftyp`.
    if head[4:8] == b"ftyp" and 8 <= int.from_bytes(head[:4], "big") <= 4096:
        brand = head[8:12]
        if brand in _FTYP_IMAGE:
            return IMAGE
        return AUDIO if brand in _FTYP_AUDIO else VIDEO
    return None


#: A paste that looks like an error, by a fixed rule, so the window can label the chip
#: «Error pegado» instead of «Texto pegado»: a traceback, a stack frame, or a failure line
#: in the shapes compilers, runtimes and tools print.
_ERROR = re.compile(
    r"Traceback \(most recent call last\)"
    r"|^\s+at \S.*[:(]\d+"
    r"|^\s*File \".+\", line \d+"
    r"|\b[A-Z]\w*(?:Error|Exception)\b"
    r"|\b(?:error|ERROR|Error)(?:\[\w+\])?:"
    r"|^\s*(?:panic|fatal|FATAL|FAIL|FAILED)\b"
    r"|\bnpm ERR!",
    re.MULTILINE,
)


#: Bytes no text file contains: below TAB, and between CR and space. Past this share of them
#: in the sample the file is binary even without a NUL. OpenCode's rule
#: (packages/opencode/src/tool/read.ts, 30% of a 4 KiB sample; read 2026-09-30).
_CONTROL = frozenset([*range(0, 9), *range(14, 32)])
_CONTROL_SHARE = 0.30


def _mostly_control(head: bytes) -> bool:
    if not head:
        return False
    return sum(1 for byte in head if byte in _CONTROL) / len(head) > _CONTROL_SHARE


def looks_like_error(text: str) -> bool:
    """Whether a paste is error output, by `_ERROR` (never the model's judgement)."""
    return _ERROR.search(text) is not None


def _lines(text: str) -> int:
    """Lines as `read` and the artifact store count them: a final line without a newline
    counts, a final newline does not start another."""
    return text.count("\n") + (0 if text.endswith("\n") or not text else 1)


@dataclass(frozen=True, slots=True)
class Refused:
    """Why something cannot be attached, and the name the window shows it under."""

    reason: str
    name: str

    def as_dict(self) -> dict[str, str]:
        return {"error": "refused", "reason": self.reason, "name": self.name}


@dataclass(frozen=True, slots=True)
class Attachment:
    """One thing attached to a message, with the text the model is sent for it."""

    #: ``file`` (a file the person chose) or ``text`` (a long paste).
    kind: str
    #: The name it is shown and sent under: a project file's path from the project root,
    #: any other file's own name, ``pasted text`` for a paste.
    name: str
    content: str
    #: The file it was read from, resolved; None for a paste.
    path: Path | None = None
    #: Whether ``path`` is inside the project.
    inside: bool = False
    #: For a document converted to text (`Convert`), what it is, e.g. "PDF, 3 pages".
    document: str | None = None
    #: The file's own size on disk, when ``content`` is its text rather than its bytes.
    size: int | None = None
    #: For an image: its format and size in pixels, and its bytes when it came pasted, with
    #: no file to read them from again.
    image: images.ImageInfo | None = None
    raw: bytes | None = field(default=None, repr=False)

    @property
    def bytes(self) -> int:
        return len(self.content.encode("utf-8"))

    @property
    def lines(self) -> int:
        return _lines(self.content)

    def facts(self) -> dict[str, Any]:
        """What the window draws the chip from (never the content)."""
        facts: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "bytes": self.bytes,
            "lines": self.lines,
        }
        if self.kind == "image" and self.image is not None:
            facts["bytes"] = self.size or 0
            facts["lines"] = 0
            facts["width"], facts["height"] = self.image.width, self.image.height
            facts["media"] = self.image.media_type
            if self.path is not None:
                facts["path"] = str(self.path)
                facts["inside"] = self.inside
        elif self.kind == "file":
            facts["path"] = str(self.path)
            facts["inside"] = self.inside
            if self.document is not None:
                facts["document"] = self.document
                facts["bytes"] = self.size if self.size is not None else self.bytes
        else:
            facts["error"] = looks_like_error(self.content)
        return facts

    def spec(self) -> dict[str, str]:
        """How the window attaches it again when a rewind puts it back on the composer: a
        file or an image by its path (read again when sent), a paste by its text."""
        if self.kind in ("file", "image") and self.path is not None:
            return {"path": str(self.path)}
        return {"text": self.content}


PASTED_NAME = "pasted text"


def _inside(path: Path, project_root: Path) -> bool:
    try:
        path.relative_to(project_root.resolve())
    except ValueError:
        return False
    return True


def read_file(
    raw: str, project_root: Path, convert: Convert | None = None
) -> Attachment | Refused:
    """The file at ``raw`` (an absolute path the person chose) as an attachment, or why it
    cannot be one. Read-only: the file is opened for reading and nothing is written.
    Symbolic links are followed, as a file manager does; the name is the link's. With
    ``convert``, a document (PDF, Word, Excel, ...) is attached as its text, or refused
    with the converter's reason."""
    name = os.path.basename(raw.rstrip("/\\")) or raw
    if not raw or "\x00" in raw or not os.path.isabs(raw):
        return Refused(UNREADABLE, name)
    candidate = Path(raw)
    try:
        resolved = candidate.resolve(strict=True)
        status = resolved.stat()
    except (OSError, RuntimeError):
        return Refused(UNREADABLE, name)
    if stat.S_ISDIR(status.st_mode):
        return Refused(FOLDER, name)
    if not stat.S_ISREG(status.st_mode):
        # A device, a pipe or a socket: reading one may never end (/dev/zero, a FIFO).
        return Refused(UNREADABLE, name)
    if status.st_size > MAX_BYTES:
        return Refused(TOO_LARGE, name)
    try:
        with resolved.open("rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError:
        return Refused(UNREADABLE, name)
    if len(data) > MAX_BYTES:
        return Refused(TOO_LARGE, name)
    head = data[:SAMPLE_BYTES]
    inside = _inside(resolved, project_root)
    if inside:
        try:
            shown = candidate.relative_to(project_root).as_posix()
        except ValueError:
            shown = resolved.relative_to(project_root.resolve()).as_posix()
    else:
        shown = name
    document = convert(resolved) if convert is not None else None
    if document is not None:
        reason = getattr(document, "reason", None)
        if reason is not None:
            return Refused(_DOCUMENT_REASONS.get(reason, CORRUPT), name)
        return Attachment(
            "file", shown, document.text, resolved, inside, document.describe(), len(data)
        )
    kind = _signature(head)
    if kind == IMAGE:
        return _image(data, shown, name, resolved, inside)
    if kind is not None:
        return Refused(kind, name)
    if b"\x00" in head or _mostly_control(head):
        return Refused(BINARY, name)
    # Decoded as `read` does (`tools.files.read_lines`): invalid UTF-8 becomes U+FFFD,
    # never an error.
    content = data.decode("utf-8", errors="replace")
    return Attachment("file", shown, content, resolved, inside)


def _image(
    data: bytes, shown: str, name: str, path: Path | None, inside: bool
) -> Attachment | Refused:
    """An image a model can be sent, or why not: a format no model here takes, or past
    the size a provider accepts."""
    info = images.sniff(data)
    if info is None:
        return Refused(IMAGE, name)
    if len(data) > images.MAX_BYTES or max(info.width, info.height) > images.MAX_SIDE:
        return Refused(IMAGE_TOO_LARGE, name)
    return Attachment(
        "image", shown, "", path, inside, size=len(data), image=info,
        raw=data if path is None else None,
    )


def read_image(data: bytes, name: str) -> Attachment | Refused:
    """An image pasted with no file behind it (a screenshot from the clipboard)."""
    return _image(data, name or "image", name or "image", None, False)


def read_text(text: str) -> Attachment | Refused:
    """A long paste as an attachment, or why it cannot be one."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        return Refused(TOO_LARGE, PASTED_NAME)
    return Attachment("text", PASTED_NAME, text)


def load(
    spec: Any, project_root: Path, convert: Convert | None = None
) -> Attachment | Refused | None:
    """One attachment as the window names it (``{"path": "/abs"}`` or ``{"text": "..."}``),
    read now; None for anything else. ``convert`` as for `read_file`."""
    if not isinstance(spec, Mapping):
        return None
    path, text = spec.get("path"), spec.get("text")
    data = spec.get("image")
    if isinstance(data, str) and path is None and text is None:
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            return None
        name = spec.get("name")
        return read_image(raw, name if isinstance(name, str) else "image")
    if isinstance(path, str) and text is None:
        return read_file(path, project_root, convert)
    if isinstance(text, str) and path is None and text.strip():
        return read_text(text)
    return None


# -- what the model is sent --------------------------------------------------------------

_TAG = {"file": "attached_file", "text": "attached_text", "image": "attached_image"}
_HEADER = re.compile(
    r"^<(?P<tag>attached_file|attached_text|attached_image) (?P<attrs>[^\n]*)>$", re.MULTILINE
)

#: What a model that cannot see images reads in place of one.
UNSEEN = (
    "The person attached this image, but the model in use cannot see images: nothing of it "
    "reaches you beyond this marker."
)
_ATTR = re.compile(r'(?P<key>[a-z]+)="(?P<value>[^"]*)"')


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "&#10;")
    )


def _unescape(value: str) -> str:
    return (
        value.replace("&#10;", "\n")
        .replace("&gt;", ">")
        .replace("&lt;", "<")
        .replace("&quot;", '"')
        .replace("&amp;", "&")
    )


def block(attachment: Attachment, copy: Path | None = None, *, seen: bool = True) -> str:
    """One attachment as the model reads it: a header with its name, size and where it is
    kept, the content exactly as read, and the closing tag. ``copy`` is where the session
    saved it (`ArtifactStore.stash`), so it can be read again after a fold has taken the
    message out of the window. An image's block is its marker only (the image itself is an
    `ImageBlock`); with ``seen`` False it says the model in use cannot see it."""
    if attachment.kind == "image" and attachment.image is not None:
        return _image_block(attachment, copy, seen)
    tag = _TAG[attachment.kind]
    attrs = [f'name="{_escape(attachment.name)}"']
    if attachment.path is not None:
        attrs.append(f'path="{_escape(str(attachment.path))}"')
    if attachment.document is not None:
        attrs.append(f'document="{_escape(attachment.document)}, as text"')
        if attachment.size is not None:
            attrs.append(f'size="{attachment.size}"')
    attrs.append(f'lines="{attachment.lines}"')
    attrs.append(f'bytes="{attachment.bytes}"')
    if copy is not None:
        attrs.append(f'copy="{_escape(str(copy))}"')
    return f"<{tag} {' '.join(attrs)}>\n{attachment.content}\n</{tag}>"


def _image_block(attachment: Attachment, copy: Path | None, seen: bool) -> str:
    info = attachment.image
    assert info is not None
    note = "" if seen else UNSEEN
    attrs = [f'name="{_escape(attachment.name)}"']
    if attachment.path is not None:
        attrs.append(f'path="{_escape(str(attachment.path))}"')
    attrs.append(f'media="{info.media_type}"')
    attrs.append(f'width="{info.width}" height="{info.height}"')
    attrs.append(f'size="{attachment.size or 0}"')
    if not seen:
        attrs.append('seen="no"')
    attrs.append(f'lines="{_lines(note)}"')
    attrs.append(f'bytes="{len(note.encode("utf-8"))}"')
    if copy is not None:
        attrs.append(f'copy="{_escape(str(copy))}"')
    return f"<attached_image {' '.join(attrs)}>\n{note}\n</attached_image>"


def compose(words: str, blocks: Sequence[str]) -> str:
    """The message the model gets: the person's words, then each attachment's block. With
    no blocks it is ``words`` itself, the same string."""
    if not blocks:
        return words
    parts = [words] if words else []
    parts.extend(blocks)
    return "\n\n".join(parts)


@dataclass(frozen=True, slots=True)
class Sent:
    """One attachment read back from a message `compose` wrote."""

    kind: str
    name: str
    content: str
    path: str | None
    copy: str | None
    #: A document sent as its text: what it is, and its own size on disk.
    document: str | None = None
    size: int | None = None
    #: An image: its media type and size in pixels, and whether the model could see it.
    media: str | None = None
    width: int = 0
    height: int = 0
    seen: bool = True

    @property
    def bytes(self) -> int:
        return len(self.content.encode("utf-8"))

    @property
    def lines(self) -> int:
        return _lines(self.content)

    def facts(self, project_root: Path | None = None) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "bytes": self.bytes,
            "lines": self.lines,
        }
        if self.kind == "image":
            facts["bytes"] = self.size or 0
            facts["lines"] = 0
            facts["width"], facts["height"] = self.width, self.height
            facts["media"] = self.media
            if not self.seen:
                facts["seen"] = False
            if self.path is not None:
                facts["path"] = self.path
                facts["inside"] = project_root is not None and _inside(
                    Path(self.path), project_root
                )
        elif self.kind == "file":
            facts["path"] = self.path
            facts["inside"] = (
                project_root is not None
                and self.path is not None
                and _inside(Path(self.path), project_root)
            )
            if self.document is not None:
                facts["document"] = self.document
                facts["bytes"] = self.size if self.size is not None else self.bytes
        else:
            facts["error"] = looks_like_error(self.content)
        return facts

    def spec(self) -> dict[str, str]:
        if self.kind == "image":
            # A pasted image has no file of its own: its copy is attached again.
            return {"path": self.path or self.copy or ""}
        return {"path": self.path} if self.kind == "file" and self.path else {"text": self.content}


def _parse_block(text: str, start: int) -> tuple[Sent, int] | None:
    """The block whose header starts at ``start``, and where it ends. None unless it is
    exactly what `block` writes: a header, ``bytes`` bytes of content, the closing tag."""
    header = _HEADER.match(text, start)
    if header is None:
        return None
    tag = header.group("tag")
    attrs = {
        m.group("key"): _unescape(m.group("value"))
        for m in _ATTR.finditer(header.group("attrs"))
    }
    try:
        size = int(attrs["bytes"])
        int(attrs["lines"])
        name = attrs["name"]
    except (KeyError, ValueError):
        return None
    body_start = header.end() + 1
    if text[header.end() : body_start] != "\n":
        return None
    encoded = text[body_start:].encode("utf-8")
    if len(encoded) < size:
        return None
    try:
        content = encoded[:size].decode("utf-8")
    except UnicodeDecodeError:
        return None
    end = body_start + len(content)
    closing = f"\n</{tag}>"
    if text[end : end + len(closing)] != closing:
        return None
    kind = {"attached_file": "file", "attached_text": "text", "attached_image": "image"}[tag]
    document = attrs.get("document")
    if document is not None:
        document = document.removesuffix(", as text")
    try:
        size = int(attrs["size"]) if "size" in attrs else None
    except ValueError:
        size = None
    if kind == "image":
        try:
            width, height = int(attrs.get("width", 0)), int(attrs.get("height", 0))
        except ValueError:
            width = height = 0
        sent = Sent(
            kind, name, content, attrs.get("path"), attrs.get("copy"), None, size,
            media=attrs.get("media"), width=width, height=height, seen=attrs.get("seen") != "no",
        )
        return sent, end + len(closing)
    sent = Sent(kind, name, content, attrs.get("path"), attrs.get("copy"), document, size)
    return sent, end + len(closing)


def split(text: str) -> tuple[str, tuple[Sent, ...]]:
    """The person's words and their attachments, from a message `compose` wrote. Any other
    message gives ``text`` itself and nothing. The attachments are the whole end of the
    message: the earliest header from which every block parses and nothing follows the
    last."""
    for header in _HEADER.finditer(text):
        start = header.start()
        if start == 0:
            words = ""
        elif text[start - 2 : start] == "\n\n":
            words = text[: start - 2]
        else:
            continue
        found: list[Sent] = []
        position = start
        while True:
            parsed = _parse_block(text, position)
            if parsed is None:
                break
            sent, position = parsed
            found.append(sent)
            if position == len(text):
                return words, tuple(found)
            if text[position : position + 2] != "\n\n":
                break
            position += 2
    return text, ()


def seed_text(text: str) -> str:
    """What a fold's seed keeps of a message: the words and one line per attachment saying
    what it was and where it is kept. Never the content, which `render_tasks` would
    otherwise carry verbatim into every handover. Any other message is returned unchanged."""
    words, sent = split(text)
    if not sent:
        return text
    lines = [words] if words else []
    for item in sent:
        where = item.copy or item.path
        if item.kind == "image":
            kept = f", kept in {where}" if where else ""
            lines.append(f"[attached image {item.name}: {item.width}x{item.height}{kept}]")
            continue
        what = "file" if item.kind == "file" else "text"
        kept = f", kept in {where}" if where else ""
        converted = f" ({item.document}, as text)" if item.document else ""
        lines.append(f"[attached {what} {item.name}{converted}: {item.lines} lines{kept}]")
    return "\n".join(lines)
