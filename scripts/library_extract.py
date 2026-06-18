#!/usr/bin/env python3
"""library_extract.py — PDF extraction for the Research Library corpus.

Single PyMuPDF-based extractor with two modes (replaces the earlier pair of
per-agent scripts ``extract_pdf.py`` + ``pdf_extract.py``):

  corpus  Structured ingestion into the Research Library: copies the source
          PDF to ``raw/``, extracts+cleans the full text, writes a chapter
          file, a citable ``meta.json``, and updates the topic + master
          indexes. This is the source of truth for primary text.

  dump    Flat full-text extraction (every page) to stdout or a file, with a
          ``pages=N chars=M`` line on stderr so the caller can confirm the
          WHOLE document was read, not a fragment. Use this for one-off
          translation / TTS narration of a PDF that is not (yet) in the corpus.

Why a dedicated script: ``read_file`` does not parse PDFs, and the agent's
``execute_code`` sandbox strips the venv so ``import fitz`` fails there. Run
this through the ``terminal`` tool with the interpreter that HAS PyMuPDF.

Paths are workspace-relative. The corpus root resolves from, in order:
  1. ``--library-root``
  2. ``$LIBRARY_ROOT``
  3. ``$HMK_WORKSPACE_ROOT/library``
  4. ``<cwd>/library``

Examples:
  ./scripts/hmk library_extract.py corpus book.pdf medicina_germanica \\
      la_gran_confusion --title "La Gran Confusión" --author "Gastón Vargas" \\
      --year 2022 --language es
  ./scripts/hmk library_extract.py dump book.pdf --out /tmp/chapter.txt

Requires: pymupdf (``pip install pymupdf``).
"""
import argparse
import collections
import json
import os
import re
import shutil
import sys
from datetime import date

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit(
        "PyMuPDF not available for this interpreter — run via the venv python "
        "that has PyMuPDF (e.g. the agent's `.venv/bin/python3`)."
    )


def resolve_library_root(explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get("LIBRARY_ROOT")
    if env:
        return os.path.abspath(env)
    ws = os.environ.get("HMK_WORKSPACE_ROOT")
    if ws:
        return os.path.join(os.path.abspath(ws), "library")
    return os.path.join(os.getcwd(), "library")


def slugify(s):
    return re.sub(r"[^a-z0-9_]", "", s.lower().replace(" ", "_").replace("-", "_"))[:60]


def clean_page_text(text, page_number, title="", author=""):
    """Drop running headers/footers and lone page numbers, join soft hyphens,
    collapse runs of spaces. Paragraph structure (blank lines) is preserved."""
    title_u, author_u = title.upper().strip(), author.upper().strip()
    cleaned = []
    for line in text.splitlines():
        stripped = line.strip()
        if title_u and stripped.upper() == title_u:
            continue
        if author_u and stripped.upper() == author_u:
            continue
        # lone page number matching this page's 1-based index
        if re.match(r"^\d{1,4}$", stripped) and int(stripped) == page_number:
            continue
        cleaned.append(line)
    out = "\n".join(cleaned)
    out = out.replace("\xad\n", "").replace("\xad", "")  # soft hyphens
    out = re.sub(r" +", " ", out)
    return out


def extract_all_text(pdf_path, title="", author="", clean=True):
    doc = fitz.open(pdf_path)
    pages = doc.page_count
    parts = []
    for p in range(pages):
        raw = doc.load_page(p).get_text("text")
        text = clean_page_text(raw, p + 1, title, author) if clean else raw
        if text.strip():
            parts.append(text)
    doc.close()
    return "\n\n".join(parts), pages


# --------------------------------------------------------------------------
# Structured (Markdown) extraction — captures document structure ONCE at
# ingestion (headings by relative font size, paragraphs), so every downstream
# consumer (TTS pauses, translation, analysis, cross-refs) reuses real
# structure instead of re-guessing it from flat text.
# --------------------------------------------------------------------------
def _doc_body_size(doc):
    """Most common span size (by char volume) — the body-text size."""
    c = collections.Counter()
    for page in doc:
        for b in page.get_text("dict").get("blocks", []):
            for line in b.get("lines", []):
                for s in line.get("spans", []):
                    t = s.get("text", "").strip()
                    if t:
                        c[round(s["size"], 1)] += len(t)
    return max(c, key=c.get) if c else 12.0


def _block_text(block):
    """Join a block's physical lines, repairing line-break hyphenation and
    soft hyphens, collapsing runs of whitespace."""
    out = ""
    for line in block.get("lines", []):
        s = "".join(span.get("text", "") for span in line.get("spans", [])).replace("\xad", "")
        if not s:
            continue
        if out.endswith("-") and s[:1].islower():
            out = out[:-1] + s            # word split across lines → rejoin
        elif out:
            out = out + " " + s
        else:
            out = s
    return re.sub(r"[ \t]+", " ", out).strip()


def _block_max_size(block):
    return max(
        (round(s["size"], 1)
         for line in block.get("lines", [])
         for s in line.get("spans", [])
         if s.get("text", "").strip()),
        default=0.0,
    )


def extract_structured_markdown(pdf_path, title="", author=""):
    """Extract a PDF to structured Markdown.

    Headings are detected by font size relative to the body size (three bands →
    ``#`` / ``##`` / ``###``); multi-line / cross-block headings of the same
    level are merged; body blocks become paragraphs. Running headers/footers
    (lines equal to the title or author) and lone page numbers are dropped.
    Returns ``(markdown, pages, n_headings)``.
    """
    doc = fitz.open(pdf_path)
    pages = doc.page_count
    body = _doc_body_size(doc)
    title_u, author_u = title.upper().strip(), author.upper().strip()

    def level_for(sz):
        if sz >= body * 1.7:
            return 1
        if sz >= body * 1.3:
            return 2
        if sz >= body * 1.1:
            return 3
        return 0  # body

    units = []  # (level, text); level 0 == paragraph
    for pno in range(pages):
        for b in doc.load_page(pno).get_text("dict").get("blocks", []):
            if b.get("type") != 0:
                continue
            txt = _block_text(b)
            if not txt:
                continue
            up = txt.upper()
            if (title_u and up == title_u) or (author_u and up == author_u):
                continue
            if re.fullmatch(r"\d{1,4}", txt):  # lone page number
                continue
            lvl = level_for(_block_max_size(b))
            # merge a heading that continued from the immediately-preceding
            # heading of the SAME level (title wrapped across blocks)
            if lvl and units and units[-1][0] == lvl:
                units[-1] = (lvl, units[-1][1] + " " + txt)
            else:
                units.append((lvl, txt))
    doc.close()

    out, n_head = [], 0
    for lvl, txt in units:
        if lvl:
            out.append(("#" * lvl) + " " + txt)
            n_head += 1
        else:
            out.append(txt)
    return "\n\n".join(out), pages, n_head


# --------------------------------------------------------------------------
# mode: dump
# --------------------------------------------------------------------------
def cmd_dump(args):
    text, pages = extract_all_text(
        args.pdf, clean=not args.raw_text
    )
    sys.stderr.write("pages=%d chars=%d\n" % (pages, len(text)))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        sys.stderr.write("written: %s\n" % args.out)
    else:
        sys.stdout.write(text)
    return 0


# --------------------------------------------------------------------------
# mode: corpus
# --------------------------------------------------------------------------
def _load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def cmd_corpus(args):
    library = resolve_library_root(args.library_root)
    topics_dir = os.path.join(library, "topics")
    topic_dir = os.path.join(topics_dir, args.topic_id)
    book_dir = os.path.join(topic_dir, "books", args.book_slug)
    for sub in ("chapters", "raw", "annotations"):
        os.makedirs(os.path.join(book_dir, sub), exist_ok=True)

    # duplicate guard
    topic_index_path = os.path.join(topic_dir, "index.json")
    topic_index = _load_json(
        topic_index_path,
        {"topic": {"id": args.topic_id, "name": args.topic_id, "description": "", "tags": []}, "books": []},
    )
    if any(b.get("slug") == args.book_slug for b in topic_index["books"]):
        sys.exit(f"book '{args.book_slug}' already exists in topic '{args.topic_id}' — remove or rename first")

    # copy original PDF to raw/ (never mutated thereafter)
    raw_rel = os.path.join("raw", os.path.basename(args.pdf))
    raw_path = os.path.join(book_dir, raw_rel)
    if not os.path.exists(raw_path):
        shutil.copy2(args.pdf, raw_path)

    if getattr(args, "flat", False):
        full_text, pages = extract_all_text(args.pdf, args.title, args.author)
        chapter_rel = os.path.join("chapters", "00_completo.txt")
        method, chapter_fmt, n_head = "pymupdf_page_text", "text", 0
    else:
        full_text, pages, n_head = extract_structured_markdown(args.pdf, args.title, args.author)
        chapter_rel = os.path.join("chapters", "00_completo.md")
        method, chapter_fmt = "pymupdf_dict_structured_markdown", "markdown"
    with open(os.path.join(book_dir, chapter_rel), "w", encoding="utf-8") as f:
        f.write(full_text)
    words = len(full_text.split())

    meta = {
        "book": {
            "title": args.title,
            "author": args.author,
            "year": args.year,
            "isbn": args.isbn,
            "language": args.language,
        },
        "source": {
            "original_filename": os.path.basename(args.pdf),
            "raw_path": raw_rel,
        },
        "extraction": {
            "method": method,
            "date": date.today().isoformat(),
            "coverage_pages": f"1-{pages}",
            "format": chapter_fmt,
            "headings": n_head,
            "cleaning": ["removed_headers_footers", "joined_soft_hyphens", "rejoined_hyphenation"],
        },
        "chapters": [
            {"num": "00", "title": "Completo", "pages_pdf": f"1-{pages}", "words": words, "file": chapter_rel}
        ],
        "annotations": {},
        "total_chapters": 1,
        "total_words": words,
    }
    _write_json(os.path.join(book_dir, "meta.json"), meta)

    # topic index
    topic_index["books"].append({
        "slug": args.book_slug,
        "title": args.title,
        "author": args.author,
        "year": args.year,
        "path": f"books/{args.book_slug}",
        "meta": f"books/{args.book_slug}/meta.json",
    })
    _write_json(topic_index_path, topic_index)

    # master index
    master_path = os.path.join(library, "index.json")
    master = _load_json(master_path, {
        "library": {
            "name": "Research Library",
            "path": library,
            "created": date.today().isoformat(),
            "schema_version": "1.0",
            "description": "Document library for research with precise bibliographic citations.",
        },
        "topics": [],
    })
    if not any(t.get("id") == args.topic_id for t in master["topics"]):
        master["topics"].append({"id": args.topic_id, "path": f"topics/{args.topic_id}", "index": f"topics/{args.topic_id}/index.json"})
        _write_json(master_path, master)

    print(json.dumps({
        "ok": True,
        "book_dir": book_dir,
        "pages": pages,
        "words": words,
        "chapter": chapter_rel,
        "meta": os.path.join(book_dir, "meta.json"),
    }, ensure_ascii=False))
    sys.stderr.write(
        f"\nNOTE: chapters/{os.path.basename(chapter_rel)} holds the WHOLE book "
        f"({n_head} headings detected, format={chapter_fmt}). Split it along the\n"
        "'#'/'##' headings into ##_titulo chapters and update meta.json, then register a\n"
        "catalog entry in memory (see the library-acquisition skill, 'Cierre de ingreso')\n"
        "so the book is discoverable via librarian/hybrid-pack.\n"
    )
    return 0


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)

    pc = sub.add_parser("corpus", help="structured ingestion into the Research Library")
    pc.add_argument("pdf")
    pc.add_argument("topic_id")
    pc.add_argument("book_slug")
    pc.add_argument("--title", required=True)
    pc.add_argument("--author", required=True)
    pc.add_argument("--year", default="")
    pc.add_argument("--isbn", default="")
    pc.add_argument("--language", default="es")
    pc.add_argument("--library-root", default=None, help="override corpus root (else $LIBRARY_ROOT / $HMK_WORKSPACE_ROOT/library)")
    pc.add_argument("--flat", action="store_true", help="legacy flat .txt extraction instead of structured Markdown")
    pc.set_defaults(func=cmd_corpus)

    pd = sub.add_parser("dump", help="flat full-text extraction to stdout/file")
    pd.add_argument("pdf")
    pd.add_argument("--out", default=None, help="write to this file instead of stdout")
    pd.add_argument("--raw-text", action="store_true", help="skip header/footer/soft-hyphen cleaning")
    pd.set_defaults(func=cmd_dump)
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    sys.exit(args.func(args))
