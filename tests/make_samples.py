"""Create sandbox sample files for LocalGate tests. Everything is generated
into a temp dir; nothing outside is touched."""

from __future__ import annotations

import json
import os
import struct
import zipfile
import zlib


def write_text(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def make_md_note(path: str, title: str, body: str) -> str:
    return write_text(path, f"# {title}\n\n{body}\n")


def make_code_file(path: str, content: str) -> str:
    return write_text(path, content)


def make_docx(path: str, paragraphs: list[str]) -> str:
    """Minimal but valid .docx via stdlib zipfile."""
    body = "".join(
        "<w:p><w:r><w:t xml:space=\"preserve\">"
        + p.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        + "</w:t></w:r></w:p>" for p in paragraphs)
    document = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        f"<w:body>{body}</w:body></w:document>")
    content_types = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
        "<Override PartName=\"/word/document.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>"
        "</Types>")
    rels = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"word/document.xml\"/>"
        "</Relationships>")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return path


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_pdf(path: str, pages: list[str]) -> str:
    """Handcrafted valid PDF with uncompressed text streams (works with both
    pypdf and the built-in fallback extractor)."""
    objects: list[bytes] = []
    # 1: catalog, 2: pages, then per page: page obj + content obj
    page_ids = []
    content_ids = []
    next_id = 3
    for _ in pages:
        page_ids.append(next_id)
        content_ids.append(next_id + 1)
        next_id += 2
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>\nendobj\n".encode())
    for i, text in enumerate(pages):
        pid, cid = page_ids[i], content_ids[i]
        objects.append(
            f"{pid} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {cid} 0 R /Resources << /Font << /F1 90 0 R >> >> >>\nendobj\n".encode())
        stream = (f"BT /F1 14 Tf 72 700 Td ({_pdf_escape(text)}) Tj ET").encode("latin-1")
        objects.append(f"{cid} 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode()
                       + stream + b"\nendstream\nendobj\n")
    objects.append(b"90 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0] * (len(objects) + 1)
    for i, obj in enumerate(objects, 1):
        offsets[i] = len(out)
        out += obj
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for i in range(1, len(objects) + 1):
        out += f"{offsets[i]:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(bytes(out))
    return path


def make_chat_export(path: str, messages: list[dict]) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"messages": messages}, f, ensure_ascii=False, indent=1)
    return path


def make_png(path: str, width: int = 300, height: int = 80,
             boxes: list[tuple[int, int, int, int]] | None = None) -> str:
    """Tiny valid PNG (stdlib only). With boxes it draws rectangles (no text)."""
    px = [[255] * width for _ in range(height)]
    for (x0, y0, x1, y1) in (boxes or [(20, 20, 60, 60), (90, 20, 130, 60)]):
        for y in range(y0, min(y1, height)):
            for x in range(x0, min(x1, width)):
                px[y][x] = 0
    raw = b"".join(b"\x00" + bytes(row) for row in px)

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    return path


def build_sample_vault(root: str) -> dict[str, str]:
    """Standard test corpus. Returns {label: path}."""
    paths: dict[str, str] = {}
    # markdown notes (EN + ZH)
    paths["md_en"] = make_md_note(
        os.path.join(root, "notes", "project-phoenix.md"), "Project Phoenix",
        "Project Phoenix is the internal machine learning platform. "
        "It processes training datasets nightly and produces model checkpoints. "
        "The quarterly OKR mentions Phoenix reliability improvements.")
    paths["md_zh"] = make_md_note(
        os.path.join(root, "notes", "读书笔记.md"), "读书笔记",
        "这是一份关于机器学习的读书笔记。深度学习是机器学习的一个分支，"
        "神经网络可以自动学习数据特征。每周三下午固定整理读书笔记。")
    # source code
    paths["code"] = make_code_file(
        os.path.join(root, "src", "pipeline.py"),
        "def process_training_data(dataset):\n"
        "    \"\"\"Clean and normalize the machine learning dataset.\"\"\"\n"
        "    rows = [r for r in dataset if r is not None]\n"
        "    return sorted(rows, key=lambda r: r.timestamp)\n")
    # plain text
    paths["txt"] = write_text(
        os.path.join(root, "docs", "meeting-notes.txt"),
        "Meeting 2026-09-01: discussed quarterly OKR. Alice will own the "
        "Phoenix reliability project. Next review on Friday.")
    # pdf
    paths["pdf"] = make_pdf(
        os.path.join(root, "docs", "architecture.pdf"),
        ["LocalGate Architecture. The gateway runs a hybrid search engine.",
         "Page two: index storage is local only, privacy first design."])
    # docx
    paths["docx"] = make_docx(
        os.path.join(root, "docs", "report.docx"),
        ["Quarterly Report", "The Phoenix platform improved reliability by 40 percent.",
         "Next quarter focus: vector search quality."])
    # chat export
    paths["chat"] = make_chat_export(
        os.path.join(root, "chats", "team-chat.json"),
        [{"sender": "alice", "time": "2026-09-01 10:00",
          "text": "Has anyone reviewed the Phoenix pull request?"},
         {"sender": "bob", "time": "2026-09-01 10:02",
          "text": "Reviewing now, the machine learning pipeline looks good."}])
    # image (rectangles only: OCR may return empty text)
    paths["img"] = make_png(os.path.join(root, "pics", "diagram.png"))
    # a file that should never be indexed (blacklisted subtree)
    paths["black_secret"] = write_text(
        os.path.join(root, "notes", "private", "secret-diary.txt"),
        "This private diary must never appear in search results.")
    # unsupported ext
    paths["binary"] = os.path.join(root, "notes", "blob.xyz")
    with open(paths["binary"], "wb") as f:
        f.write(b"\x00\x01\x02not a known format")
    return paths
