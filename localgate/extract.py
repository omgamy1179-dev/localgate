"""Text extraction for supported local formats. READ-ONLY: opens user files 'rb' only.

Supported:
- Markdown / plain text / source code (utf-8, gbk fallback)
- PDF (.pdf) via pypdf when available, built-in minimal extractor as fallback
- Word (.docx) via zip + XML (stdlib only)
- Chat export JSON (generic message-list shape) -> "[ts] sender: text" lines
- Images -> delegated to the local OCR engine (see ocr.py)

Every parser is bounded (decompressed bytes, entries/pages, output characters)
so hostile or corrupt files cannot exhaust memory or disk. Size/complexity
overruns raise ExtractError or truncate (recorded by the caller) - a single
bad file never takes the service down.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
import zlib

TEXT_EXTS = {".md", ".markdown", ".txt", ".text", ".log", ".csv", ".tsv", ".rst"}
CODE_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h", ".cpp", ".hpp",
             ".go", ".rs", ".rb", ".sh", ".bash", ".zsh", ".sql", ".html", ".css",
             ".swift", ".kt", ".php", ".yml", ".yaml", ".toml", ".ini", ".conf",
             ".xml", ".json"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp",
              ".heic", ".heif"}
CHAT_EXPORT_EXTS = {".json"}

INDEXABLE_EXTS = TEXT_EXTS | CODE_EXTS | IMAGE_EXTS | CHAT_EXPORT_EXTS | {".pdf", ".docx"}

# -------------------------------------------------------------- resource caps
MAX_INPUT_BYTES = 256 * 1024 * 1024      # hard ceiling on any raw read
MAX_DOCX_ENTRIES = 4096                  # zip entries examined per docx
MAX_DOCX_XML_BYTES = 64 * 1024 * 1024    # decompressed document.xml ceiling
MAX_PDF_PAGES = 2000                     # pypdf pages parsed per pdf
MAX_PDF_STREAMS = 4096                   # streams decompressed by the fallback
MAX_PDF_DECOMPRESSED = 64 * 1024 * 1024  # decompressed stream ceiling (bomb guard)
MAX_EXTRACTED_CHARS = 2 * 1024 * 1024    # output text kept per document
MAX_CHAT_JSON_BYTES = 64 * 1024 * 1024   # chat-export json read ceiling


class ExtractError(Exception):
    pass


def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _cap_chars(text: str) -> tuple[str, bool]:
    """Truncate extracted text to MAX_EXTRACTED_CHARS; report truncation."""
    if len(text) <= MAX_EXTRACTED_CHARS:
        return text, False
    return text[:MAX_EXTRACTED_CHARS], True


def read_file_bytes(path: str, max_bytes: int = MAX_INPUT_BYTES) -> bytes:
    size = os.path.getsize(path)
    if size > max_bytes:
        raise ExtractError(f"file exceeds read cap of {max_bytes} bytes")
    with open(path, "rb") as f:
        return f.read(max_bytes + 1)


# ---------------------------------------------------------------- text/plain

def extract_text_like(path: str) -> str:
    return decode_bytes(read_file_bytes(path))


# ---------------------------------------------------------------- chat export

_MSG_TEXT_KEYS = ("text", "content", "message", "body", "msg")
_MSG_SENDER_KEYS = ("sender", "author", "user", "name", "from", "talker", "speaker")
_MSG_TIME_KEYS = ("ts", "time", "timestamp", "date", "datetime", "create_time")


def _msg_field(msg: dict, keys: tuple[str, ...]):
    for k in keys:
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return ""


def looks_like_chat_export(obj) -> bool:
    if isinstance(obj, list) and obj:
        return all(isinstance(m, dict) and _msg_field(m, _MSG_TEXT_KEYS) for m in obj[:20])
    if isinstance(obj, dict):
        for key in ("messages", "msgs", "chat", "items", "records"):
            v = obj.get(key)
            if isinstance(v, list) and v and looks_like_chat_export(v):
                return True
    return False


def format_chat_export(obj) -> str:
    msgs = obj
    if isinstance(obj, dict):
        for key in ("messages", "msgs", "chat", "items", "records"):
            v = obj.get(key)
            if isinstance(v, list):
                msgs = v
                break
    lines: list[str] = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        text = _msg_field(m, _MSG_TEXT_KEYS)
        if not text:
            continue
        sender = _msg_field(m, _MSG_SENDER_KEYS)
        ts = _msg_field(m, _MSG_TIME_KEYS)
        prefix = f"[{ts}] " if ts else ""
        who = f"{sender}: " if sender else ""
        lines.append(f"{prefix}{who}{text}")
    return "\n".join(lines)


def extract_chat_json(path: str) -> str:
    try:
        data = json.loads(decode_bytes(read_file_bytes(path, MAX_CHAT_JSON_BYTES)))
    except ValueError as e:
        raise ExtractError(f"invalid chat json: {e}") from e
    if not looks_like_chat_export(data):
        # not a chat shape: fall back to pretty JSON text
        return json.dumps(data, ensure_ascii=False, indent=1)
    return format_chat_export(data)


# ---------------------------------------------------------------- docx

_W_T = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.S)
_W_P_END = re.compile(r"</w:p>")


def extract_docx(path: str) -> str:
    """Extract text from a .docx, bounded against zip bombs: entry count,
    declared and actual decompressed sizes are all capped."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if len(names) > MAX_DOCX_ENTRIES:
                raise ExtractError(
                    f"docx has too many entries ({len(names)} > {MAX_DOCX_ENTRIES})")
            if "word/document.xml" not in names:
                raise ExtractError("invalid docx: word/document.xml missing")
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise ExtractError(
                    f"docx document.xml too large ({info.file_size} > "
                    f"{MAX_DOCX_XML_BYTES} bytes)")
            with z.open("word/document.xml") as f:
                xml_bytes = f.read(MAX_DOCX_XML_BYTES + 1)
            if len(xml_bytes) > MAX_DOCX_XML_BYTES:
                raise ExtractError("docx document.xml exceeds cap")
    except zipfile.BadZipFile as e:
        raise ExtractError(f"invalid docx: {e}") from e
    except zlib.error as e:
        raise ExtractError(f"corrupt docx stream: {e}") from e
    return _docx_xml_to_text(xml_bytes.decode("utf-8", errors="replace"))


def _docx_xml_to_text(xml: str) -> str:
    parts: list[str] = []
    buf: list[str] = []
    events = []
    for m in _W_T.finditer(xml):
        events.append((m.start(), "t", m.group(1)))
    for m in _W_P_END.finditer(xml):
        events.append((m.start(), "pend", None))
    events.sort(key=lambda e: e[0])
    for _, kind, val in events:
        if kind == "t":
            val = val.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            buf.append(val)
        else:
            line = "".join(buf).strip()
            if line:
                parts.append(line)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return "\n".join(parts)


# ---------------------------------------------------------------- pdf

def _extract_pdf_pypdf(path: str) -> str:
    try:
        import logging
        logging.getLogger("pypdf").setLevel(logging.ERROR)
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        raise ExtractError("pypdf not installed") from None
    try:
        reader = PdfReader(path)
        n_pages = len(reader.pages)
        if n_pages > MAX_PDF_PAGES:
            raise ExtractError(
                f"pdf has too many pages ({n_pages} > {MAX_PDF_PAGES})")
        pages = []
        budget = MAX_EXTRACTED_CHARS
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            pages.append(text[:budget])
            budget -= len(text)
            if budget <= 0:
                break
        return "\n\n".join(pages)
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError(f"pdf parse failed: {e}") from e


_PDF_TEXT_OPS = re.compile(
    rb"\((?:\\.|[^\\()])*\)\s*Tj|\[(?:[^\[\]\\]|\\.)*\]\s*TJ", re.S)
_PDF_LITERAL = re.compile(rb"\((?:\\.|[^\\()])*\)", re.S)
_PDF_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"(": b"(", b")": b")", b"\\": b"\\"}


def _pdf_unescape(lit: bytes) -> str:
    body = lit[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i:i + 1]
        if c == b"\\" and i + 1 < len(body):
            nxt = body[i + 1:i + 2]
            if nxt in _PDF_ESCAPES:
                out += _PDF_ESCAPES[nxt]
                i += 2
                continue
            if nxt.isdigit():
                oct_digits = body[i + 1:i + 4]
                j = 0
                while j < len(oct_digits) and oct_digits[j:j + 1].isdigit() and j < 3:
                    j += 1
                try:
                    out.append(int(oct_digits[:j], 8) & 0xFF)
                except ValueError:
                    pass
                i += 1 + j
                continue
            out += nxt
            i += 2
            continue
        out += c
        i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return out.decode("gb18030")
        except UnicodeDecodeError:
            return out.decode("latin-1")


def _extract_pdf_minimal(path: str) -> str:
    """Dependency-free fallback: find streams, decompress, pull Tj/TJ literals.
    Covers simple uncompressed & Flate-compressed PDFs (incl. our test PDFs).
    Bounded: stream count, decompressed size and output length are capped."""
    data = read_file_bytes(path)
    chunks: list[str] = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        if len(chunks) > MAX_PDF_STREAMS:
            break
        raw = m.group(1)
        if len(raw) > MAX_PDF_DECOMPRESSED:
            continue
        try:
            candidates = (raw, *_try_flate(raw))
        except ExtractError:
            continue  # over-cap stream: skip it, keep the rest
        for candidate in candidates:
            text = _pdf_ops_to_text(candidate)
            if text.strip():
                chunks.append(text)
                break
    if not chunks:
        # not stream-wrapped (fully plain pdf): scan whole file
        text = _pdf_ops_to_text(data)
        if text.strip():
            chunks.append(text)
    if not chunks:
        raise ExtractError("no extractable pdf text found")
    return "\n\n".join(chunks)


def _try_flate(raw: bytes) -> list[bytes]:
    """Decompress a Flate stream with a hard output cap (zip-bomb guard)."""
    out = []
    d = zlib.decompressobj()
    try:
        data = d.decompress(raw, MAX_PDF_DECOMPRESSED + 1)
        while data:
            out.append(data)
            if sum(len(x) for x in out) > MAX_PDF_DECOMPRESSED:
                raise ExtractError("pdf stream exceeds decompression cap")
            if d.eof or not d.unconsumed_tail:
                break
            data = d.decompress(d.unconsumed_tail, MAX_PDF_DECOMPRESSED + 1)
    except zlib.error:
        pass
    return out


def _pdf_ops_to_text(stream: bytes) -> str:
    parts: list[str] = []
    for op in _PDF_TEXT_OPS.finditer(stream):
        seg = op.group(0)
        if seg.rstrip().endswith(b"TJ"):
            for lit in _PDF_LITERAL.findall(seg):
                parts.append(_pdf_unescape(lit))
        else:
            for lit in _PDF_LITERAL.findall(seg):
                parts.append(_pdf_unescape(lit))
        parts.append("\n")
    return "".join(parts)


def extract_pdf(path: str) -> str:
    try:
        return _extract_pdf_pypdf(path)
    except ExtractError:
        return _extract_pdf_minimal(path)


# ---------------------------------------------------------------- dispatcher

def classify(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext == ".docx":
        return "docx"
    if ext in CHAT_EXPORT_EXTS:
        return "chat_json"
    if ext in CODE_EXTS:
        return "code"
    if ext in TEXT_EXTS:
        return "text"
    return "unknown"


def extract(path: str, kind: str | None = None) -> tuple[str, str, dict]:
    """Return (text, kind, extra). Raises ExtractError on hard failures.

    Returns empty text (not an error) for binary-ish payloads that simply have
    no text; the caller decides how to record that. Output text is capped at
    MAX_EXTRACTED_CHARS; extra["truncated"] is True when the cap clipped it.
    """
    kind = kind or classify(path)
    extra: dict = {}
    text = _extract_by_kind(path, kind)
    text, truncated = _cap_chars(text)
    if truncated:
        extra["truncated"] = True
    return text, kind, extra


def _extract_by_kind(path: str, kind: str) -> str:
    if kind == "image":
        raise ExtractError("images are handled by the OCR pipeline")
    if kind == "pdf":
        return extract_pdf(path)
    if kind == "docx":
        return extract_docx(path)
    if kind == "chat_json":
        try:
            return extract_chat_json(path)
        except (ValueError, ExtractError) as e:
            raise ExtractError(f"chat json parse failed: {e}") from e
    if kind in ("text", "code"):
        return extract_text_like(path)
    raise ExtractError(f"unsupported file kind: {kind}")
