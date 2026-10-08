#!/usr/bin/env python3
"""Extract disclosure text/tables and add a chunk FTS index to metadata.db."""

from __future__ import annotations

import argparse
import html as html_std
import json
import re
import shutil
import sqlite3
import sys
import unicodedata
from pathlib import Path

from lxml import html


SPACE_RE = re.compile(r"[\t\r\f\v ]+")
BLANK_RE = re.compile(r"\n{3,}")
SOURCE_EXTENSIONS = {".xml", ".html"}


def clean(value: str) -> str:
    value = html_std.unescape(value).replace("\xa0", " ")
    value = unicodedata.normalize("NFC", value)
    value = SPACE_RE.sub(" ", value)
    value = re.sub(r" *\n *", "\n", value)
    return BLANK_RE.sub("\n\n", value).strip()


def decode_document(raw: bytes) -> str:
    for encoding in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def extract_lines(path: Path) -> list[str]:
    text = decode_document(path.read_bytes())
    parser = html.HTMLParser(encoding="utf-8", recover=True, remove_comments=True)
    root = html.fromstring(text.encode("utf-8"), parser=parser)
    for bad in root.xpath("//script|//style|//noscript|//svg"):
        bad.drop_tree()

    lines: list[str] = []
    heading_tags = {"document-name", "company-name", "cover-title", "title", "h1", "h2", "h3", "h4"}
    paragraph_tags = {"p", "li"}

    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        tag = element.tag.lower()
        if tag == "tr":
            cells = []
            for cell in element.xpath("./td|./th|./te|./tu"):
                cell_text = clean(" ".join(cell.itertext()))
                if cell_text:
                    cells.append(cell_text)
            line = " | ".join(cells)
            if line:
                lines.append(line)
        elif tag in heading_tags:
            value = clean(" ".join(element.itertext()))
            if value:
                lines.append(f"[제목] {value}")
        elif tag in paragraph_tags and not any(
            isinstance(a.tag, str) and a.tag.lower() in {"table", "tr"}
            for a in element.iterancestors()
        ):
            value = clean(" ".join(element.itertext()))
            if value:
                lines.append(value)

    result: list[str] = []
    for line in lines:
        if not result or result[-1] != line:
            result.append(line)
    return result


def split_long_line(line: str, limit: int) -> list[str]:
    if len(line) <= limit:
        return [line]
    parts = []
    while len(line) > limit:
        cut = line.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(line[:cut].strip())
        line = line[cut:].strip()
    if line:
        parts.append(line)
    return parts


def make_chunks(lines: list[str], target_chars: int, overlap_lines: int) -> list[str]:
    expanded = [piece for line in lines for piece in split_long_line(line, target_chars)]
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    latest_heading = ""

    for line in expanded:
        if line.startswith("[제목]"):
            latest_heading = line
        extra = len(line) + (1 if current else 0)
        if current and current_size + extra > target_chars:
            chunks.append("\n".join(current))
            carry = current[-overlap_lines:] if overlap_lines else []
            if latest_heading and latest_heading not in carry:
                carry.insert(0, latest_heading)
            current = carry
            current_size = sum(len(x) + 1 for x in current)
        current.append(line)
        current_size += extra
    if current:
        chunks.append("\n".join(current))
    return [c for c in chunks if c.strip()]


def create_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        DROP TABLE IF EXISTS chunks_fts;
        DROP TABLE IF EXISTS chunks;
        CREATE TABLE chunks (
            chunk_id INTEGER PRIMARY KEY,
            doc_id TEXT NOT NULL,
            source_file TEXT NOT NULL,
            chunk_no INTEGER NOT NULL,
            text TEXT NOT NULL,
            search_meta TEXT NOT NULL,
            FOREIGN KEY (doc_id) REFERENCES documents(doc_id)
        );
        CREATE INDEX idx_chunks_doc ON chunks(doc_id, chunk_no);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            text,
            search_meta,
            content='chunks',
            content_rowid='chunk_id',
            tokenize='unicode61'
        );
        """
    )


def build(corpus: Path, metadata: Path, output: Path, target_chars: int) -> None:
    corpus, metadata, output = corpus.resolve(), metadata.resolve(), output.resolve()
    temp = output.with_suffix(output.suffix + ".building")
    temp.unlink(missing_ok=True)
    shutil.copy2(metadata, temp)
    db = sqlite3.connect(temp)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    create_schema(db)

    docs = db.execute(
        """SELECT doc_id, corp_name, listed_name, stock_code, industry, sector,
                  report_nm, doc_group, doc_subtype, rcept_dt, base_year, base_month,
                  source_files_json
             FROM documents ORDER BY rcept_dt, doc_id"""
    ).fetchall()
    inserted = 0
    indexed_files = 0
    skipped_pdf = 0
    errors: list[str] = []

    try:
        for doc_no, doc in enumerate(docs, 1):
            (
                doc_id, corp_name, listed_name, stock_code, industry, sector,
                report_nm, doc_group, doc_subtype, rcept_dt, base_year, base_month,
                source_files_json,
            ) = doc
            meta = " ".join(
                str(x) for x in (
                    corp_name, listed_name, stock_code, industry, sector, report_nm,
                    doc_group, doc_subtype or "", rcept_dt, base_year or "", base_month or "",
                ) if x != ""
            )
            per_doc_chunk_no = 0
            for source_rel in json.loads(source_files_json):
                source = corpus / source_rel
                if source.suffix.lower() not in SOURCE_EXTENSIONS:
                    skipped_pdf += 1
                    continue
                try:
                    lines = extract_lines(source)
                    chunks = make_chunks(lines, target_chars=target_chars, overlap_lines=2)
                except Exception as exc:
                    errors.append(f"{source_rel}: {type(exc).__name__}: {exc}")
                    continue
                for chunk in chunks:
                    cur = db.execute(
                        "INSERT INTO chunks(doc_id, source_file, chunk_no, text, search_meta) VALUES(?,?,?,?,?)",
                        (doc_id, source_rel, per_doc_chunk_no, chunk, meta),
                    )
                    db.execute(
                        "INSERT INTO chunks_fts(rowid, text, search_meta) VALUES(?,?,?)",
                        (cur.lastrowid, chunk, meta),
                    )
                    inserted += 1
                    per_doc_chunk_no += 1
                indexed_files += 1

            if doc_no % 100 == 0:
                db.commit()
                print(f"PROGRESS documents={doc_no}/{len(docs)} files={indexed_files} chunks={inserted}", flush=True)

        db.commit()
        db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        db.commit()
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        stats = {
            "documents": len(docs),
            "indexed_files": indexed_files,
            "chunks": inserted,
            "skipped_pdf": skipped_pdf,
            "errors": len(errors),
            "integrity": integrity,
        }
        print(json.dumps(stats, ensure_ascii=False), flush=True)
        if errors:
            print("EXTRACTION_ERRORS", file=sys.stderr)
            print("\n".join(errors[:30]), file=sys.stderr)
        if integrity != "ok" or inserted == 0 or len(errors) > 10:
            raise RuntimeError(f"Content index validation failed: {stats}")
    finally:
        db.close()

    temp.replace(output)
    wal = temp.with_name(temp.name + "-wal")
    shm = temp.with_name(temp.name + "-shm")
    wal.unlink(missing_ok=True)
    shm.unlink(missing_ok=True)
    print(f"CONTENT_INDEX_OK {output} ({output.stat().st_size:,} bytes)", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-chars", type=int, default=2200)
    args = parser.parse_args()
    build(args.corpus, args.metadata, args.output, args.target_chars)


if __name__ == "__main__":
    main()
