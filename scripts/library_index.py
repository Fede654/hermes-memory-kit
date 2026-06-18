#!/usr/bin/env python3
"""Index a Research Library book's sections into HMK memory (``library.db``).

Each Markdown section (a ``##`` heading + its body) of an ingested book becomes
one HMK chapter — embedded and tagged ``corpus/section/<topic>/<slug>`` — so
cross-section and cross-book retrieval ("cruces") works out of the box: the
agent reaches sections via ``hybrid-pack``/``search`` and ``expand`` jumps to
the on-disk Markdown. This is the substrate for analysis/cross-references; it
does NOT pre-compute any analysis. Idempotent (``replace`` by title), so it's
safe to re-run after re-segmentation.

Run from the workspace root via the ``hmk`` wrapper (loads the agent .env):
    ./scripts/hmk library_index.py topics/<topic>/books/<slug>/meta.json
Then refresh embeddings:
    ./scripts/hmk memoryctl.py embed-backfill
"""
import argparse
import json
import os
import re
import sys


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import memoryctl

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("meta", help="path to the book's meta.json")
    ap.add_argument("--shelf", default="evidence")
    ap.add_argument("--min-words", type=int, default=40,
                    help="skip section stubs (e.g. bare part dividers) shorter than this")
    ap.add_argument("--importance", type=float, default=0.6)
    a = ap.parse_args()

    meta_path = os.path.abspath(a.meta)
    meta = json.load(open(meta_path, encoding="utf-8"))
    book_dir = os.path.dirname(meta_path)
    book = meta.get("book", {})
    btitle = book.get("title") or os.path.basename(book_dir)
    parts = book_dir.split(os.sep)
    slug = parts[-1] if parts else ""
    topic = parts[-3] if len(parts) >= 3 else ""

    indexed, skipped = 0, 0
    for ch in meta.get("chapters", []):
        rel = ch.get("file")
        if not rel:
            continue
        fpath = os.path.join(book_dir, rel)
        if not os.path.exists(fpath):
            continue
        raw = open(fpath, encoding="utf-8").read().strip()
        # body = section minus its leading heading line, for the length gate
        body = re.sub(r"^#{1,6}\s+.*$", "", raw, count=1, flags=re.M).strip()
        if len(body.split()) < a.min_words:
            skipped += 1
            continue
        sect = (ch.get("title") or "").strip() or str(ch.get("num", ""))
        title = f"{btitle} — {sect}"
        memoryctl.add_text(
            shelf_name=a.shelf,
            title=title,
            raw=raw,
            tags=["corpus", "section", topic, slug],
            importance=a.importance,
            source_path=fpath,
            source_kind="corpus-section",
            replace=True,
        )
        indexed += 1

    print(json.dumps(
        {"ok": True, "indexed": indexed, "skipped": skipped,
         "book": btitle, "topic": topic, "slug": slug},
        ensure_ascii=False))


if __name__ == "__main__":
    main()
