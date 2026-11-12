"""Documents a person keeps (PDF, Word, Excel, PowerPoint, OpenDocument) as text.

One converter with two callers: the `read` tool, for a document already in the project,
and the attachments the person drops on the window (`context.attachments`, which gets this
module from the composition root because `context` sits below `tools`). Both get the same
text, so a model reads a document the same way however it arrived. The text is plain, with
Markdown only where structure carries meaning (a heading, a list item, a table), and a line
marking each page, sheet or slide.

A document is recognised by its bytes, never its name: a PDF by its signature, the others
by what their ZIP package holds (`[Content_Types].xml` with ``word/``, ``xl/`` or ``ppt/``;
an OpenDocument ``mimetype``). Plain-text formats (CSV, HTML, Markdown) are not converted:
they are already text, and a project's ``index.html`` is source the model may have to edit.

Pasted text that was too large for the window has a third caller, the document index in
the app (`cli._documents_from_offloads`): `from_text` turns a pasted web page into the text
a browser shows, so it can be indexed and read back a line at a time.

What cannot be turned into text is reported, never guessed at (`Unreadable`):

- ``encrypted``: a password is needed (a PDF that opens with the empty password is read);
- ``scanned``: a PDF whose pages hold almost no text, i.e. images of pages. OCR is not in
  the MVP, and a model told "no text" will not invent the contents;
- ``corrupt``: the package or the PDF cannot be parsed;
- ``too_large``: over `MAX_BYTES`, or a package that inflates past `MAX_INFLATED` (a ZIP bomb).

Sources, read 2026-09-30 (cogito-lab experiments/2026-09-30-attachments/RESEARCH.md has the
versions and sizes): Microsoft's markitdown covers the same formats with one library per
format (pdfminer.six, mammoth, openpyxl and pandas, python-pptx), tens of megabytes with
native wheels, and reads no OpenDocument; docling brings a layout model. OOXML and
OpenDocument are XML in a ZIP, which the standard library reads, so they are read with it,
with headings, lists and tables as Markdown. Only PDF, which needs a real parser, takes a
dependency: pypdfium2 (Apache-2.0/BSD-3, PDFium's own wheels, 8.1 MB installed). It had the
best text in py-pdf/benchmarks (97 %, against pypdf's 96 %) at a tenth of a second per
document there, and it opens an AES owner-password PDF without a separate crypto package.
"""

from __future__ import annotations

import html
import re
import threading
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

#: Which conversion this module performs. The conversion cache (`tools.document_cache`)
#: keys every entry by it, so bump it in the same commit as any change that can alter what
#: `extract` returns for the same bytes (a marker, a table's layout, a threshold, a
#: reason). Every document is then converted again rather than served stale.
CONVERTER_VERSION = 1

#: The largest document accepted, in bytes: the design's limit for an attachment (1n).
MAX_BYTES = 20_000_000
#: The most a package may inflate to, all its parts together; past it, a ZIP bomb.
MAX_INFLATED = 200_000_000
#: A PDF of pictures whose pages average fewer printable characters than this is a scan:
#: the little text there is is a page number or a stamp, not the document.
SCANNED_CHARS_PER_PAGE = 16
#: How many times a repeated non-empty row or cell of an OpenDocument sheet is expanded at
#: most. An empty one is kept once: a sheet that repeats an empty row a million times
#: means "the rest is empty".
_MAX_REPEAT = 1000

KINDS = ("pdf", "docx", "xlsx", "pptx", "odt", "ods", "odp", "office")

#: What each kind is called in a result's header.
NAMES = {
    "pdf": "PDF",
    "docx": "Word document",
    "xlsx": "Excel workbook",
    "pptx": "PowerPoint presentation",
    "odt": "OpenDocument text",
    "ods": "OpenDocument spreadsheet",
    "odp": "OpenDocument presentation",
    # A password-protected Word, Excel or PowerPoint file: not a ZIP at all but an OLE
    # compound file holding an `EncryptedPackage` stream ([MS-OFFCRYPTO] 2.3.4.4), which
    # does not say which of the three it is.
    "office": "Office document",
    # Text the person pasted or attached that was too large for the window (`from_text`).
    "text": "Text",
    "html": "Web page",
}

_CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENCRYPTED_PACKAGE = "EncryptedPackage".encode("utf-16-le")

_ODF_MIMETYPES = {
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.presentation": "odp",
}


@dataclass(frozen=True, slots=True)
class Document:
    """A document as text, and what it is made of."""

    kind: str
    text: str
    #: Pages of a PDF; slides of a presentation.
    pages: int | None = None
    #: The sheets of a workbook, in order.
    sheets: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> str:
        """"PDF, 12 pages", "Excel workbook, 3 sheets (Ventas, Costos, Resumen)"."""
        name = NAMES[self.kind]
        if self.sheets:
            count = len(self.sheets)
            shown = ", ".join(self.sheets[:8]) + (", ..." if count > 8 else "")
            return f"{name}, {count} sheet{'s' if count != 1 else ''} ({shown})"
        if self.pages is not None:
            unit = "slide" if self.kind in ("pptx", "odp") else "page"
            return f"{name}, {self.pages} {unit}{'s' if self.pages != 1 else ''}"
        return name


@dataclass(frozen=True, slots=True)
class Unreadable:
    """A document that cannot be turned into text, and why: ``encrypted``, ``scanned``,
    ``corrupt`` or ``too_large``."""

    kind: str
    reason: str
    detail: str = ""


# -- the parts of one ---------------------------------------------------------------------


#: The marker lines `extract` writes, one before each page, slide or sheet.
_PAGE_MARK = re.compile(r"^\[(?P<unit>page|slide) (?P<number>\d+) of \d+\]$")
_SHEET_MARK = re.compile(r"^\[sheet (?P<number>\d+): (?P<name>.*)\]$")
#: Lines per part at most. A part is what an answer cites, so it must be small enough to
#: read back in one window.
PART_LINES = 200
#: Characters per part at most, with or without page markers. A part is one Estella ingest:
#: at the 5.8 ms a character measured on this machine (cogito-lab
#: experiments/2026-09-30-doc-cache) 4,000 take about 25 s, inside Estella's call timeout.
#: The 40,000 allowed before took about 230 s, so such a part was abandoned and its
#: document never recorded as indexed (context-fit critique D2).
PART_MAX_CHARS = 4_000


@dataclass(frozen=True, slots=True)
class Unit:
    """One page, slide or sheet of a document's text (or a run of lines, where the
    document has none), and where it sits in the text `read` shows."""

    #: "page 37", "slide 4", "sheet 2: Ventas", "lines 1-200".
    label: str
    #: 1-based lines of the text, as `read` numbers them: its marker line, when it has one,
    #: through its last line.
    first_line: int
    last_line: int
    #: Its text, without the marker line.
    text: str


def units(document: Document) -> tuple[Unit, ...]:
    """``document``'s text cut where its own markers cut it: pages, slides, sheets. A page
    or sheet longer than `PART_MAX_CHARS` is cut further, and a document without markers
    is cut into runs of `PART_LINES` lines, each labelled with the lines it spans."""
    lines = document.text.rstrip("\n").split("\n")
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if page := _PAGE_MARK.match(line):
            starts.append((index, f"{page.group('unit')} {page.group('number')}"))
        elif sheet := _SHEET_MARK.match(line):
            starts.append((index, f"sheet {sheet.group('number')}: {sheet.group('name')}"))
    found: list[Unit] = []
    if not starts:
        for begin, end in _runs(lines):
            chunk = lines[begin:end]
            found.append(Unit(f"lines {begin + 1}-{end}", begin + 1, end, "\n".join(chunk)))
        return tuple(found)
    for position, (index, label) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        body = lines[index + 1 : end]
        text = "\n".join(body)
        if len(text) <= PART_MAX_CHARS:
            found.append(Unit(label, index + 1, end, text.strip("\n")))
            continue
        for begin, stop in _runs(body):
            chunk = body[begin:stop]
            first = index + 2 + begin
            last = first + len(chunk) - 1
            found.append(Unit(f"{label}, lines {first}-{last}", first, last, "\n".join(chunk)))
    return tuple(found)


def _runs(lines: list[str]) -> list[tuple[int, int]]:
    """``lines`` cut into runs of at most `PART_LINES` lines and `PART_MAX_CHARS`
    characters, as ``(begin, end)`` indexes. A single line longer than that is a run of its
    own."""
    runs: list[tuple[int, int]] = []
    begin = 0
    size = 0
    for index, line in enumerate(lines):
        cost = len(line) + 1
        if index > begin and (index - begin >= PART_LINES or size + cost > PART_MAX_CHARS + 1):
            runs.append((begin, index))
            begin, size = index, 0
        size += cost
    if begin < len(lines) or not runs:
        runs.append((begin, len(lines)))
    return runs


# -- pasted text ------------------------------------------------------------------------------

#: The longest line `from_text` leaves, so `read` shows each one whole.
TEXT_LINE_CHARS = 2_000
#: Markup is taken for a page when it starts like one, or when at least this share of its
#: characters are inside tags.
_TAG_SHARE = 0.3
_TAG = re.compile(r"<[^<>]{1,1000}>")
#: Elements that start a new line of visible text.
_BLOCK_TAGS = frozenset(
    (
        "address", "article", "aside", "blockquote", "br", "caption", "dd", "details",
        "dialog", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
        "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
        "pre", "section", "summary", "table", "tbody", "tfoot", "thead", "tr", "ul",
    )
)
#: Elements whose content a reader never sees.
_HIDDEN_TAGS = frozenset({"script", "style", "noscript", "template", "title"})
_CELL_TAGS = frozenset({"td", "th"})
_SPACES = re.compile(r"[ \t\f\v\u00a0]+")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
#: Scripts and styles, taken out before parsing. One never closed is taken out to the end
#: of its line only: `HTMLParser`, like a browser, would hide the rest of the page in it,
#: and a pasted page cut short can end inside one.
_CLOSED_CODE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_OPEN_CODE = re.compile(r"<(?:script|style)\b[^>]*>[^\n]*", re.IGNORECASE)
#: The longest name `headline` gives.
_HEADLINE_CHARS = 60


class _Visible(HTMLParser):
    """The text a browser shows: no scripts, styles or comments, entities unescaped, and a
    line break at each block element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pieces: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden += 1
        elif tag in _BLOCK_TAGS:
            self.pieces.append("\n")
        elif tag in _CELL_TAGS:
            self.pieces.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden = max(0, self._hidden - 1)
        elif tag in _BLOCK_TAGS:
            self.pieces.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.pieces.append(data)


def _is_html(text: str) -> bool:
    start = text.lstrip()[:15].lower()
    if start.startswith(("<!doctype html", "<html")):
        return True
    if not text:
        return False
    inside = sum(len(match.group()) for match in _TAG.finditer(text))
    return inside / len(text) >= _TAG_SHARE


def _wrap(line: str) -> list[str]:
    """``line`` in pieces of at most `TEXT_LINE_CHARS`, cut at a space where there is one."""
    pieces: list[str] = []
    while len(line) > TEXT_LINE_CHARS:
        cut = line.rfind(" ", 1, TEXT_LINE_CHARS + 1)
        if cut <= 0:
            cut = TEXT_LINE_CHARS
        pieces.append(line[:cut])
        line = line[cut:].lstrip(" ")
    pieces.append(line)
    return pieces


def from_text(text: str) -> Document:
    """Pasted text in a form `read` and the document index can use. A page of HTML becomes
    the text a browser shows (`_Visible`, kind ``html``); anything else is kept as it is
    (kind ``text``). Either way no line is longer than `TEXT_LINE_CHARS`: a pasted page can
    hold a line of hundreds of thousands of characters, which no `read` shows whole."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if _is_html(text):
        parser = _Visible()
        parser.feed(_OPEN_CODE.sub("", _CLOSED_CODE.sub("", text)))
        parser.close()
        lines = [_SPACES.sub(" ", line).strip() for line in "".join(parser.pieces).split("\n")]
        kept = [piece for line in lines if line for piece in _wrap(line)]
        return Document("html", "\n".join(kept) + "\n")
    return Document("text", "\n".join(piece for line in text.split("\n") for piece in _wrap(line)))


def headline(original: str, document: Document) -> str:
    """A short name for pasted text: a page's title, or else its first line of text, at
    most `_HEADLINE_CHARS` characters. Empty when it has neither."""
    found = ""
    if document.kind == "html" and (title := _TITLE.search(original)) is not None:
        found = _SPACES.sub(" ", html.unescape(title.group(1)).replace("\n", " ")).strip()
    if not found:
        lines = (" ".join(line.split()) for line in document.text.split("\n"))
        found = next((line for line in lines if line), "")
    if len(found) > _HEADLINE_CHARS:
        found = found[: _HEADLINE_CHARS - 1].rstrip() + "…"
    return found


# -- web pages ----------------------------------------------------------------------------------

#: Elements that carry no text a person reads. Not their children either.
_DEAD_TAGS = frozenset(
    {"script", "style", "template", "noscript", "svg", "iframe", "canvas", "object", "embed",
     "head", "input", "select", "textarea", "button", "option", "datalist", "dialog"}
)
#: Page furniture. `header` is dropped only outside the main content, where it holds the
#: site's menu; inside an `article` it holds the title.
_FURNITURE_TAGS = frozenset({"nav", "footer", "aside"})
_FURNITURE_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary", "search"})
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
     "source", "track", "wbr"}
)
#: A start tag that closes an element a browser would close for it: `<li>` ends the open
#: `<li>`, a `<td>` the open cell, and so on. Looked up down the open elements, up to the
#: first of the listed stoppers.
_IMPLIED_END = {
    "li": (frozenset({"li"}), frozenset({"ul", "ol"})),
    "dt": (frozenset({"dt", "dd"}), frozenset({"dl"})),
    "dd": (frozenset({"dt", "dd"}), frozenset({"dl"})),
    "tr": (frozenset({"tr", "td", "th"}), frozenset({"table", "tbody", "thead", "tfoot"})),
    "td": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "th": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "p": (frozenset({"p"}), frozenset({"div", "li", "td", "th", "blockquote", "section"})),
}
_HEADINGS = {f"h{n}": "#" * n for n in range(1, 7)}
#: Anchor texts that only mark a heading (Sphinx's and Docusaurus' permalink).
_PERMALINK_TEXT = frozenset({"¶", "#", "§", "🔗"})
_DISPLAY_NONE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_LANGUAGE = re.compile(
    r"(?:language|lang|highlight(?:-source)?)[-_:]([A-Za-z0-9_+#-]+)|brush:\s*([A-Za-z0-9_+#-]+)"
)
_MOUNT_IDS = frozenset({"root", "__next", "app", "___gatsby", "__nuxt"})
_NOSCRIPT_JS = re.compile(r"javascript", re.IGNORECASE)
#: Text shorter than this with this much HTML around it is a page that builds itself with
#: script (design 2.3).
_JS_TEXT_CHARS = 300
_JS_HTML_CHARS = 20_000
#: Most links and blocks kept from one page. A page of a million links is still one page.
_MAX_LINKS = 400


@dataclass(slots=True)
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list[_Node | str] = field(default_factory=list)
    parent: _Node | None = None


class _Tree(HTMLParser):
    """HTML as a tree, forgiving of the way real pages close (or do not close) things."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self._open = self.root
        self.title = ""
        self.meta_title = ""
        self._in_title = False
        self.noscript_text = ""
        self._in_noscript = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = not self.title
            return
        if tag == "meta":
            found = {name: value or "" for name, value in attrs}
            if found.get("property") == "og:title" and not self.meta_title:
                self.meta_title = found.get("content", "").strip()
            return
        if tag == "noscript":
            self._in_noscript = True
        rule = _IMPLIED_END.get(tag)
        if rule is not None:
            closes, stops = rule
            node = self._open
            while node is not self.root and node.tag not in stops:
                if node.tag in closes:
                    self._open = node.parent or self.root
                    break
                node = node.parent or self.root
        node = _Node(tag, {name: value or "" for name, value in attrs}, parent=self._open)
        self._open.children.append(node)
        if tag not in _VOID_TAGS:
            self._open = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag == "noscript":
            self._in_noscript = False
        node = self._open
        while node is not self.root:
            if node.tag == tag:
                self._open = node.parent or self.root
                return
            node = node.parent or self.root

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif self._in_noscript:
            self.noscript_text += data
        else:
            self._open.children.append(data)


def _hidden(node: _Node) -> bool:
    attrs = node.attrs
    return (
        "hidden" in attrs
        or attrs.get("aria-hidden", "").lower() == "true"
        or bool(_DISPLAY_NONE.search(attrs.get("style", "")))
    )


def _find(node: _Node, accept) -> _Node | None:
    """The first element, in document order, that ``accept`` takes and is not hidden."""
    for child in node.children:
        if isinstance(child, str) or _hidden(child) or child.tag in _DEAD_TAGS:
            continue
        if accept(child):
            return child
        found = _find(child, accept)
        if found is not None:
            return found
    return None


def _plain(node: _Node) -> str:
    """All the text under ``node``, whitespace folded."""
    pieces: list[str] = []

    def walk(current: _Node) -> None:
        for child in current.children:
            if isinstance(child, str):
                pieces.append(child)
            elif child.tag not in _DEAD_TAGS and not _hidden(child):
                walk(child)

    walk(node)
    return " ".join("".join(pieces).split())


@dataclass(frozen=True, slots=True)
class Readable:
    """A web page as the text a person reads."""

    title: str
    #: Markdown: headings as ``#`` lines, code as fenced blocks, links as ``[text](url)``.
    text: str
    #: Every http(s) link kept in the text, absolute, without fragments, in page order.
    links: tuple[str, ...]
    #: The page builds its text with script, which is not run.
    needs_javascript: bool


class _Writer:
    """The tree as Markdown."""

    def __init__(self, base_url: str, furniture_inside: bool) -> None:
        self.base_url = base_url
        self.blocks: list[str] = []
        self.links: dict[str, None] = {}
        #: Whether a `<header>` is dropped too (no main content was found to hold one).
        self.drop_headers = furniture_inside

    # -- inline ---------------------------------------------------------------------------

    def inline(self, node: _Node) -> str:
        pieces: list[str] = []
        for child in node.children:
            if isinstance(child, str):
                pieces.append(child.replace("\n", " "))
                continue
            if self.skip(child):
                continue
            pieces.append(self.inline_element(child))
        return "".join(pieces)

    def inline_element(self, node: _Node) -> str:
        tag = node.tag
        if tag == "br":
            return "\n"
        if tag == "a":
            return self.link(node)
        if tag in ("code", "kbd", "samp", "tt"):
            text = " ".join(_plain(node).split())
            return f"`{text}`" if text else ""
        if tag in ("strong", "b"):
            text = self.inline(node).strip()
            return f"**{text}**" if text else ""
        if tag == "img":
            return ""
        if tag in _HEADINGS or tag in _BLOCK_TAGS:
            # A block inside an inline run (a `<div>` in a link): its text, set apart.
            return " " + self.inline(node).strip() + " "
        return self.inline(node)

    def link(self, node: _Node) -> str:
        text = " ".join(self.inline(node).replace("\n", " ").split())
        href = node.attrs.get("href", "").strip()
        if not text or text in _PERMALINK_TEXT:
            return ""
        absolute = _absolute(href, self.base_url)
        if absolute is None:
            return text
        if len(self.links) < _MAX_LINKS:
            self.links.setdefault(absolute, None)
        return f"[{text}]({absolute})"

    # -- blocks ---------------------------------------------------------------------------

    def skip(self, node: _Node) -> bool:
        if node.tag in _DEAD_TAGS or _hidden(node):
            return True
        if node.tag in _FURNITURE_TAGS or node.attrs.get("role", "") in _FURNITURE_ROLES:
            return True
        return node.tag == "header" and self.drop_headers

    def emit(self, text: str) -> None:
        text = text.strip("\n")
        if text.strip():
            self.blocks.append(text)

    def flush_inline(self, run: list[str], indent: str = "") -> None:
        text = "".join(run)
        run.clear()
        lines = [" ".join(line.split()) for line in text.split("\n")]
        joined = "\n".join(indent + line for line in lines if line)
        if joined:
            self.emit(joined)

    def walk(self, node: _Node, indent: str = "") -> None:
        """Children of ``node``: block elements as blocks, anything else as one run."""
        run: list[str] = []
        for child in node.children:
            if isinstance(child, str):
                run.append(child.replace("\n", " "))
                continue
            if self.skip(child):
                continue
            if child.tag in _HEADINGS:
                self.flush_inline(run, indent)
                text = " ".join(self.inline(child).replace("\n", " ").split())
                if text:
                    self.emit(f"{_HEADINGS[child.tag]} {text}")
            elif child.tag == "pre":
                self.flush_inline(run, indent)
                self.pre(child, indent)
            elif child.tag in ("ul", "ol"):
                self.flush_inline(run, indent)
                self.render_list(child, indent)
            elif child.tag == "table":
                self.flush_inline(run, indent)
                self.table(child)
            elif child.tag == "blockquote":
                self.flush_inline(run, indent)
                start = len(self.blocks)
                self.walk(child, indent)
                for position in range(start, len(self.blocks)):
                    self.blocks[position] = "\n".join(
                        "> " + line for line in self.blocks[position].split("\n")
                    )
            elif child.tag in ("dt",):
                self.flush_inline(run, indent)
                text = self.inline(child).strip()
                if text:
                    self.emit(f"**{' '.join(text.split())}**")
            elif child.tag in _BLOCK_TAGS:
                self.flush_inline(run, indent)
                self.walk(child, indent)
            else:
                run.append(self.inline_element(child))
        self.flush_inline(run, indent)

    def pre(self, node: _Node, indent: str) -> None:
        language = self.language(node)
        pieces: list[str] = []

        def collect(current: _Node) -> None:
            for child in current.children:
                if isinstance(child, str):
                    pieces.append(child)
                elif child.tag == "br":
                    pieces.append("\n")
                elif child.tag not in _DEAD_TAGS and not _hidden(child):
                    collect(child)

        collect(node)
        body = "".join(pieces).replace("\r\n", "\n").strip("\n").rstrip()
        if not body.strip():
            return
        fence = "```"
        while fence in body:
            fence += "`"
        text = f"{fence}{language}\n{body}\n{fence}"
        if indent:
            text = "\n".join(indent + line for line in text.split("\n"))
        self.emit(text)

    @staticmethod
    def language(node: _Node) -> str:
        candidates = [node.attrs.get("class", ""), node.attrs.get("data-lang", "")]
        for child in node.children:
            if isinstance(child, _Node) and child.tag == "code":
                candidates.append(child.attrs.get("class", ""))
        parent = node.parent
        for _ in range(3):
            if parent is None:
                break
            candidates.append(parent.attrs.get("class", ""))
            parent = parent.parent
        for candidate in candidates:
            if match := _LANGUAGE.search(candidate):
                name = (match.group(1) or match.group(2)).lower()
                if name not in ("default", "none", "nohighlight", "block"):
                    return name
        return ""

    def render_list(self, node: _Node, indent: str) -> None:
        lines = self.list_lines(node, indent)
        if lines:
            self.emit("\n".join(lines))

    def list_lines(self, node: _Node, indent: str) -> list[str]:
        ordered = node.tag == "ol"
        lines: list[str] = []
        number = 0
        for child in node.children:
            if not isinstance(child, _Node) or self.skip(child) or child.tag != "li":
                continue
            number += 1
            marker = f"{number}. " if ordered else "- "
            own = _Node("li", {}, [c for c in child.children if _not_list(c)])
            text = " ".join(self.inline(own).replace("\n", " ").split())
            if text:
                lines.append(f"{indent}{marker}{text}")
            for sub in child.children:
                if isinstance(sub, _Node) and sub.tag in ("ul", "ol") and not self.skip(sub):
                    lines.extend(self.list_lines(sub, indent + "  "))
        return lines

    def table(self, node: _Node) -> None:
        rows: list[list[str]] = []

        def collect(current: _Node) -> None:
            for child in current.children:
                if not isinstance(child, _Node) or self.skip(child):
                    continue
                if child.tag == "tr":
                    cells = [
                        " ".join(self.inline(cell).replace("\n", " ").split())
                        for cell in child.children
                        if isinstance(cell, _Node)
                        and cell.tag in ("td", "th")
                        and not self.skip(cell)
                    ]
                    if cells:
                        rows.append(cells)
                elif child.tag in ("thead", "tbody", "tfoot"):
                    collect(child)

        collect(node)
        text = _table(rows)
        if text:
            self.emit(text)


def _not_list(child: _Node | str) -> bool:
    return isinstance(child, str) or child.tag not in ("ul", "ol")


#: Elements that start a block of their own when met among inline text.
_BLOCK_TAGS = frozenset(
    {"address", "article", "body", "caption", "center", "dd", "details", "div", "dl",
     "fieldset", "figcaption", "figure", "form", "header", "hgroup", "html", "li", "main",
     "p", "section", "summary", "thead", "tbody", "tfoot", "tr", "td", "th", "hr"}
)


def _absolute(href: str, base_url: str) -> str | None:
    """``href`` as an absolute http(s) address without its fragment, or None for anything
    else (a fragment, ``javascript:``, ``mailto:``, a page-local anchor)."""
    if not href or href.startswith("#"):
        return None
    try:
        joined = urljoin(base_url, href)
        parts = urlsplit(joined)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def readable_html(source: str, base_url: str = "") -> Readable:
    """A web page as the text a person reads (design 2.3, decision D5: the standard
    library, no extractor package).

    The main content is `<main>`, else `<article>`, else an element with ``role=main``, else
    the body less its navigation, header, footer and aside. Hidden elements go, which also
    drops text placed on a page to be read by an assistant and not by a person.
    """
    tree = _Tree()
    tree.feed(source)
    tree.close()
    root = tree.root
    main = (
        _find(root, lambda n: n.tag == "main")
        or _find(root, lambda n: n.tag == "article")
        or _find(root, lambda n: n.attrs.get("role") == "main")
    )
    writer = _Writer(base_url, furniture_inside=main is None)
    writer.walk(main if main is not None else root)
    text = "\n\n".join(writer.blocks).strip()
    text += "\n" if text else ""
    title = " ".join((tree.title or tree.meta_title).split())
    if not title:
        heading = _find(main or root, lambda n: n.tag == "h1")
        title = _plain(heading) if heading is not None else ""
    plain = len(text.strip())
    empty_mount = _find(
        root, lambda n: n.attrs.get("id") in _MOUNT_IDS and not _plain(n)
    ) is not None
    needs_script = plain < _JS_TEXT_CHARS and len(source) > _JS_HTML_CHARS and (
        bool(_NOSCRIPT_JS.search(tree.noscript_text)) or empty_mount
    )
    return Readable(title, text, tuple(writer.links), needs_script)


# -- recognising one ------------------------------------------------------------------


def kind_of(path: Path, head: bytes | None = None) -> str | None:
    """Which document ``path`` is, by its bytes; None for anything else (a plain-text file,
    an image, a ZIP that is not a document). ``head`` is its first bytes, when already read."""
    if head is None:
        try:
            with path.open("rb") as handle:
                head = handle.read(8)
        except OSError:
            return None
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(_CFB):
        try:
            with path.open("rb") as handle:
                return "office" if _ENCRYPTED_PACKAGE in handle.read(1_000_000) else None
        except OSError:
            return None
    if not head.startswith(b"PK\x03\x04"):
        return None
    try:
        with zipfile.ZipFile(path) as package:
            names = set(package.namelist())
            if "mimetype" in names:
                mimetype = package.read("mimetype")[:100].decode("ascii", "replace").strip()
                return _ODF_MIMETYPES.get(mimetype)
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError):
        return None
    if "[Content_Types].xml" not in names:
        return None
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    return None


def extract(path: Path) -> Document | Unreadable | None:
    """``path`` as text; why it cannot be; or None when it is not a document at all."""
    kind = kind_of(path)
    if kind is None:
        return None
    try:
        size = path.stat().st_size
    except OSError as exc:
        return Unreadable(kind, "corrupt", str(exc))
    if size > MAX_BYTES:
        return Unreadable(kind, "too_large", f"{size:,} bytes; the most is {MAX_BYTES:,}")
    if kind == "pdf":
        return _pdf(path)
    if kind == "office":
        return Unreadable(kind, "encrypted", "a password is needed to open it")
    try:
        with zipfile.ZipFile(path) as package:
            inflated = sum(info.file_size for info in package.infolist())
            if inflated > MAX_INFLATED:
                return Unreadable(kind, "too_large", f"inflates to {inflated:,} bytes")
            if any(info.flag_bits & 0x1 for info in package.infolist()):
                return Unreadable(kind, "encrypted", "the package is password-protected")
            if kind == "docx":
                return _docx(package)
            if kind == "xlsx":
                return _xlsx(package)
            if kind == "pptx":
                return _pptx(package)
            return _odf(package, kind)
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, ValueError, OSError) as exc:
        return Unreadable(kind, "corrupt", _short(exc))


def _short(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.splitlines()[0][:200]


# -- PDF ------------------------------------------------------------------------------------


#: PDFium is not thread-safe ("pypdfium2 is not thread-safe", pypdfium2 5.13 docs, Python
#: API). The core reads attachments on one thread and runs `read` on another, so every PDF
#: is read under this lock, one at a time.
_PDFIUM = threading.Lock()
#: PDFium's own error codes (fpdfview.h): the file is not a PDF or is damaged; it needs a
#: password; its security handler is one PDFium does not support.
_ERR_FORMAT, _ERR_PASSWORD, _ERR_SECURITY = 3, 4, 5
#: A page object that is an image (fpdf_edit.h, ``FPDF_PAGEOBJ_IMAGE``).
_IMAGE_OBJECT = 3


def _pdf(path: Path) -> Document | Unreadable:
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover - a dependency of the package
        return Unreadable("pdf", "corrupt", "no PDF reader is installed")
    with _PDFIUM:
        try:
            # A PDF with only an owner password (no printing, no copying) opens as every
            # viewer opens it; one that needs a password to be opened at all does not.
            document = pdfium.PdfDocument(str(path))
        except pdfium.PdfiumError as exc:
            code = getattr(exc, "err_code", None)
            if code in (_ERR_PASSWORD, _ERR_SECURITY):
                return Unreadable("pdf", "encrypted", "a password is needed to open it")
            return Unreadable("pdf", "corrupt", _short(exc))
        try:
            pages = len(document)
            parts: list[str] = []
            printable = 0
            pictured = 0
            for index in range(pages):
                page = document[index]
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_bounded().replace("\r\n", "\n").replace("\r", "\n")
                    if any(True for _ in page.get_objects(filter=[_IMAGE_OBJECT], max_depth=3)):
                        pictured += 1
                finally:
                    textpage.close()
                    page.close()
                text = text.strip()
                printable += len(re.sub(r"\s", "", text))
                parts.append(f"[page {index + 1} of {pages}]")
                if text:
                    parts.append(text)
        except pdfium.PdfiumError as exc:
            return Unreadable("pdf", "corrupt", _short(exc))
        finally:
            document.close()
    if pages == 0:
        return Unreadable("pdf", "corrupt", "it has no pages")
    # No text at all is no text, whatever the pages hold; a little text over pages of
    # pictures is a scan with a stray caption or page number, which is not the document.
    if printable == 0 or (printable < SCANNED_CHARS_PER_PAGE * pages and pictured):
        return Unreadable(
            "pdf",
            "scanned",
            f"{printable} characters of text in {pages} page(s), {pictured} of them pictures",
        )
    return Document("pdf", "\n\n".join(parts) + "\n", pages=pages)


# -- XML helpers ------------------------------------------------------------------------------


def _local(tag: str) -> str:
    """A tag without its namespace: ``{urn:...}p`` -> ``p``."""
    return tag.rsplit("}", 1)[-1]


def _attr(element: ElementTree.Element, name: str) -> str | None:
    """An attribute by its local name, whatever its namespace."""
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return None


_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _rel_id(element: ElementTree.Element) -> str:
    """The relationship an element points through (``r:id``), never a plain ``id``, which
    a slide's ``sldId`` also has."""
    return element.get(_REL_ID) or ""


def _xml(package: zipfile.ZipFile, name: str) -> ElementTree.Element:
    return ElementTree.fromstring(package.read(name))


def _table(rows: list[list[str]]) -> str:
    """Rows as a Markdown table, the first row as its header; cells on one line each."""
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    clean = [
        [cell.replace("|", "\\|").replace("\n", " ").strip() for cell in row]
        + [""] * (width - len(row))
        for row in rows
    ]
    lines = ["| " + " | ".join(clean[0]) + " |", "|" + " --- |" * width]
    lines.extend("| " + " | ".join(row) + " |" for row in clean[1:])
    return "\n".join(lines)


def _rels(package: zipfile.ZipFile, name: str) -> dict[str, str]:
    """A part's relationships: id -> target, resolved against the part's own folder."""
    folder, _, base = name.rpartition("/")
    rels_name = f"{folder}/_rels/{base}.rels" if folder else f"_rels/{base}.rels"
    if rels_name not in package.namelist():
        return {}
    found: dict[str, str] = {}
    for rel in _xml(package, rels_name):
        target = rel.get("Target") or ""
        if target.startswith("/"):
            resolved = target.lstrip("/")
        else:
            parts = [p for p in folder.split("/") if p] if folder else []
            for piece in target.split("/"):
                if piece == "..":
                    if parts:
                        parts.pop()
                elif piece and piece != ".":
                    parts.append(piece)
            resolved = "/".join(parts)
        found[rel.get("Id") or ""] = resolved
    return found


# -- Word ------------------------------------------------------------------------------------

_HEADING = re.compile(r"^(?:heading|título|titulo|überschrift|titre)\s*(\d)$", re.IGNORECASE)


def _docx_text(element: ElementTree.Element) -> str:
    out: list[str] = []
    for node in element.iter():
        tag = _local(node.tag)
        if tag == "t" and node.text:
            out.append(node.text)
        elif tag == "tab":
            out.append("\t")
        elif tag in ("br", "cr"):
            out.append("\n")
    return "".join(out)


def _docx_paragraph(paragraph: ElementTree.Element) -> str:
    text = _docx_text(paragraph).strip()
    if not text:
        return ""
    style = ""
    listed = False
    for node in paragraph:
        if _local(node.tag) != "pPr":
            continue
        for prop in node:
            if _local(prop.tag) == "pStyle":
                style = _attr(prop, "val") or ""
            elif _local(prop.tag) == "numPr":
                listed = True
    found = _HEADING.match(style.replace("-", " ").strip())
    if style.lower() == "title":
        return f"# {text}"
    if found:
        return f"{'#' * min(6, int(found.group(1)))} {text}"
    if listed or style.lower().startswith("list"):
        return f"- {text}"
    return text


def _docx_table(table: ElementTree.Element) -> str:
    rows: list[list[str]] = []
    for row in table:
        if _local(row.tag) != "tr":
            continue
        cells = []
        for cell in row:
            if _local(cell.tag) == "tc":
                paragraphs = (p for p in cell.iter() if _local(p.tag) == "p")
                cells.append(" ".join(_docx_text(p).strip() for p in paragraphs))
        rows.append(cells)
    return _table(rows)


def _docx(package: zipfile.ZipFile) -> Document:
    root = _xml(package, "word/document.xml")
    body = next((node for node in root if _local(node.tag) == "body"), root)
    blocks: list[str] = []
    for node in body:
        tag = _local(node.tag)
        if tag == "p":
            block = _docx_paragraph(node)
        elif tag == "tbl":
            block = _docx_table(node)
        elif tag == "sdt":
            # A content control (a table of contents, a cover page): its paragraphs.
            block = "\n".join(
                text for p in node.iter() if _local(p.tag) == "p" and (text := _docx_paragraph(p))
            )
        else:
            block = ""
        if block:
            blocks.append(block)
    return Document("docx", "\n\n".join(blocks) + "\n")


# -- Excel -----------------------------------------------------------------------------------

_CELL = re.compile(r"^([A-Z]+)(\d+)$")


def _column(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + (ord(letter) - 64)
    return number - 1


def _shared_strings(package: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in package.namelist():
        return []
    strings: list[str] = []
    for item in _xml(package, "xl/sharedStrings.xml"):
        if _local(item.tag) != "si":
            continue
        # A rich string is runs (r/t); its phonetic guide (rPh) is not part of the text.
        strings.append(
            "".join(
                t.text or ""
                for t in item.iter()
                if _local(t.tag) == "t" and not _inside_phonetic(item, t)
            )
        )
    return strings


def _inside_phonetic(root: ElementTree.Element, target: ElementTree.Element) -> bool:
    for node in root:
        if _local(node.tag) == "rPh" and any(child is target for child in node.iter()):
            return True
    return False


def _xlsx_sheet(root: ElementTree.Element, strings: list[str]) -> list[list[str]]:
    rows: dict[int, dict[int, str]] = {}
    for row in root.iter():
        if _local(row.tag) != "row":
            continue
        for cell in row:
            if _local(cell.tag) != "c":
                continue
            where = _CELL.match(cell.get("r") or "")
            if where is None:
                continue
            kind = cell.get("t") or "n"
            value = ""
            for part in cell:
                local = _local(part.tag)
                if local == "v" and part.text is not None:
                    value = part.text
                elif local == "is":
                    value = "".join(t.text or "" for t in part.iter() if _local(t.tag) == "t")
            if kind == "s" and value:
                index = int(value)
                value = strings[index] if 0 <= index < len(strings) else ""
            elif kind == "b":
                value = "TRUE" if value == "1" else "FALSE" if value == "0" else value
            if value != "":
                rows.setdefault(int(where.group(2)), {})[_column(where.group(1))] = value
    if not rows:
        return []
    width = max(max(cells) for cells in rows.values()) + 1
    return [[rows[r].get(c, "") for c in range(width)] for r in sorted(rows)]


def _xlsx(package: zipfile.ZipFile) -> Document:
    workbook = _xml(package, "xl/workbook.xml")
    rels = _rels(package, "xl/workbook.xml")
    strings = _shared_strings(package)
    names: list[str] = []
    blocks: list[str] = []
    for sheet in workbook.iter():
        if _local(sheet.tag) != "sheet":
            continue
        name = sheet.get("name") or f"Sheet{len(names) + 1}"
        target = rels.get(_rel_id(sheet))
        names.append(name)
        blocks.append(f"[sheet {len(names)}: {name}]")
        if target and target in package.namelist():
            table = _table(_xlsx_sheet(_xml(package, target), strings))
            if table:
                blocks.append(table)
    return Document("xlsx", "\n\n".join(blocks) + "\n", sheets=tuple(names))


# -- PowerPoint ------------------------------------------------------------------------------


def _drawing_text(root: ElementTree.Element) -> list[str]:
    """The paragraphs of a slide's shapes (DrawingML ``a:p``), in document order."""
    out: list[str] = []
    for paragraph in root.iter():
        if _local(paragraph.tag) != "p" or not paragraph.tag.startswith("{http://schemas.openxmlformats.org/drawingml"):
            continue
        text = "".join(
            (node.text or "") if _local(node.tag) == "t" else "\n"
            for node in paragraph.iter()
            if _local(node.tag) in ("t", "br")
        ).strip()
        if text:
            out.append(text)
    return out


def _pptx(package: zipfile.ZipFile) -> Document:
    presentation = _xml(package, "ppt/presentation.xml")
    rels = _rels(package, "ppt/presentation.xml")
    slides = [
        rels.get(_rel_id(slide))
        for slide in presentation.iter()
        if _local(slide.tag) == "sldId"
    ]
    blocks: list[str] = []
    for number, target in enumerate(slides, start=1):
        blocks.append(f"[slide {number} of {len(slides)}]")
        if target and target in package.namelist():
            blocks.extend(_drawing_text(_xml(package, target)))
    return Document("pptx", "\n\n".join(blocks) + "\n", pages=len(slides))


# -- OpenDocument ----------------------------------------------------------------------------


def _odf_text(element: ElementTree.Element) -> str:
    """A paragraph's text: spans, ``text:s`` runs of spaces, tabs and line breaks."""
    out: list[str] = []

    def walk(node: ElementTree.Element) -> None:
        if node.text:
            out.append(node.text)
        for child in node:
            tag = _local(child.tag)
            if tag == "s":
                out.append(" " * int(_attr(child, "c") or 1))
            elif tag == "tab":
                out.append("\t")
            elif tag == "line-break":
                out.append("\n")
            elif tag in ("note", "annotation"):
                pass
            else:
                walk(child)
            if child.tail:
                out.append(child.tail)

    walk(element)
    return "".join(out)


def _repeat(element: ElementTree.Element, name: str) -> int:
    try:
        return max(1, int(_attr(element, name) or 1))
    except ValueError:
        return 1


def _odf_table(table: ElementTree.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.iter():
        if _local(row.tag) != "table-row":
            continue
        cells: list[str] = []
        for cell in row:
            if _local(cell.tag) not in ("table-cell", "covered-table-cell"):
                continue
            text = "\n".join(_odf_text(p) for p in cell if _local(p.tag) in ("p", "h"))
            repeat = _repeat(cell, "number-columns-repeated")
            cells.extend([text] * min(repeat, _MAX_REPEAT if text else 1))
        while cells and not cells[-1].strip():
            cells.pop()
        repeat = _repeat(row, "number-rows-repeated")
        rows.extend([cells] * (min(repeat, _MAX_REPEAT) if cells else 1))
    return rows


def _odf_blocks(element: ElementTree.Element) -> Iterator[str]:
    for node in element:
        tag = _local(node.tag)
        if tag == "h":
            level = int(_attr(node, "outline-level") or 1)
            text = _odf_text(node).strip()
            if text:
                yield f"{'#' * min(6, max(1, level))} {text}"
        elif tag == "p":
            text = _odf_text(node).strip()
            if text:
                yield text
        elif tag == "list":
            for item in node.iter():
                if _local(item.tag) == "p":
                    text = _odf_text(item).strip()
                    if text:
                        yield f"- {text}"
        elif tag == "table":
            table = _table(_odf_table(node))
            if table:
                yield table
        elif tag in ("section", "frame", "text-box", "g", "custom-shape"):
            yield from _odf_blocks(node)


def _slide_text(page: ElementTree.Element) -> Iterator[str]:
    """Every heading and paragraph on a slide, in whatever shape or table holds it; the
    speaker's notes are not the slide."""
    for node in page:
        tag = _local(node.tag)
        if tag == "notes":
            continue
        if tag in ("p", "h"):
            text = _odf_text(node).strip()
            if text:
                yield text
        elif tag == "list":
            for text in _slide_text(node):
                yield text if text.startswith("- ") else f"- {text}"
        else:
            yield from _slide_text(node)


def _odf(package: zipfile.ZipFile, kind: str) -> Document:
    root = _xml(package, "content.xml")
    body = next((node for node in root.iter() if _local(node.tag) == "body"), root)
    content = next(iter(body), body)
    blocks: list[str] = []
    if kind == "ods":
        names: list[str] = []
        for table in content:
            if _local(table.tag) != "table":
                continue
            name = _attr(table, "name") or f"Sheet{len(names) + 1}"
            names.append(name)
            blocks.append(f"[sheet {len(names)}: {name}]")
            rendered = _table(_odf_table(table))
            if rendered:
                blocks.append(rendered)
        return Document("ods", "\n\n".join(blocks) + "\n", sheets=tuple(names))
    if kind == "odp":
        pages = [node for node in content if _local(node.tag) == "page"]
        for number, page in enumerate(pages, start=1):
            blocks.append(f"[slide {number} of {len(pages)}]")
            blocks.extend(_slide_text(page))
        return Document("odp", "\n\n".join(blocks) + "\n", pages=len(pages))
    blocks.extend(_odf_blocks(content))
    return Document("odt", "\n\n".join(blocks) + "\n")
