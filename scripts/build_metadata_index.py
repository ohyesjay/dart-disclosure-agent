#!/usr/bin/env python3
"""Build a Unicode-safe SQLite metadata index for the disclosure corpus."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import unicodedata
from pathlib import Path


def nfc(value: object) -> str:
    return unicodedata.normalize("NFC", "" if value is None else str(value))


def actual_path_map(corpus: Path) -> dict[str, str]:
    """Map NFC relative directory paths to their byte-for-byte filesystem paths."""
    result: dict[str, str] = {}
    raw = corpus / "raw"
    for current, dirs, _files in os.walk(raw):
        current_path = Path(current)
        rel = current_path.relative_to(corpus).as_posix()
        key = nfc(rel)
        previous = result.get(key)
        if previous is not None and previous != rel:
            raise RuntimeError(f"Unicode-normalized path collision: {previous!r} vs {rel!r}")
        result[key] = rel
    return result


def load_companies(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{k: nfc(v) for k, v in row.items()} for row in csv.DictReader(handle)]


def create_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE companies (
            corp_code TEXT PRIMARY KEY,
            stock_code TEXT NOT NULL,
            corp_name TEXT NOT NULL,
            listed_name TEXT NOT NULL,
            corp_eng_name TEXT,
            market TEXT,
            industry TEXT,
            sector_no INTEGER,
            sector TEXT,
            listing_date TEXT,
            fiscal_month TEXT,
            market_cap INTEGER
        );

        CREATE TABLE documents (
            doc_id TEXT PRIMARY KEY,
            corp_code TEXT NOT NULL,
            corp_name TEXT NOT NULL,
            listed_name TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            industry TEXT,
            sector TEXT,
            doc_group TEXT NOT NULL,
            doc_subtype TEXT,
            report_nm TEXT NOT NULL,
            is_correction INTEGER NOT NULL,
            rcept_no TEXT NOT NULL,
            rcept_dt TEXT NOT NULL,
            flr_nm TEXT,
            base_year INTEGER,
            base_month INTEGER,
            file_path_manifest TEXT NOT NULL,
            file_path_actual TEXT NOT NULL,
            file_format TEXT NOT NULL,
            n_files INTEGER NOT NULL,
            source_files_json TEXT NOT NULL,
            FOREIGN KEY (corp_code) REFERENCES companies(corp_code)
        );

        CREATE INDEX idx_docs_company ON documents(corp_code, rcept_dt DESC);
        CREATE INDEX idx_docs_stock ON documents(stock_code, rcept_dt DESC);
        CREATE INDEX idx_docs_period ON documents(corp_code, base_year, base_month);
        CREATE INDEX idx_docs_type ON documents(doc_group, doc_subtype, rcept_dt DESC);
        CREATE INDEX idx_docs_correction ON documents(corp_code, is_correction, rcept_dt DESC);

        CREATE VIRTUAL TABLE documents_fts USING fts5(
            doc_id UNINDEXED,
            corp_name,
            listed_name,
            report_nm,
            doc_subtype,
            flr_nm,
            industry,
            sector,
            tokenize='unicode61'
        );
        """
    )


def build(corpus: Path, output: Path) -> None:
    corpus = corpus.resolve()
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.unlink(missing_ok=True)

    path_map = actual_path_map(corpus)
    companies = load_companies(corpus / "universe.csv")
    missing: list[str] = []
    inserted = 0

    db = sqlite3.connect(temp)
    try:
        create_schema(db)
        db.executemany(
            """INSERT INTO companies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    c["corp_code"], c["stock_code"], c["corp_name"], c["listed_name"],
                    c.get("corp_eng_name"), c.get("market"), c.get("industry"),
                    int(c["sector_no"]) if c.get("sector_no") else None,
                    c.get("sector"), c.get("listing_date"), c.get("fiscal_month"),
                    int(c["market_cap"]) if c.get("market_cap") else None,
                )
                for c in companies
            ],
        )

        with (corpus / "manifest.jsonl").open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                manifest_rel = nfc(row["file_path"])
                actual_rel = path_map.get(manifest_rel)
                if actual_rel is None:
                    missing.append(f"line {line_no}: {manifest_rel}")
                    continue

                source_dir = corpus / actual_rel
                source_files = sorted(
                    p.relative_to(corpus).as_posix()
                    for p in source_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in {".xml", ".html", ".pdf"}
                )
                values = (
                    nfc(row["doc_id"]), nfc(row["corp_code"]), nfc(row["corp_name"]),
                    nfc(row["listed_name"]), nfc(row["stock_code"]), nfc(row.get("industry")),
                    nfc(row.get("sector")), nfc(row["doc_group"]), nfc(row.get("doc_subtype")),
                    nfc(row["report_nm"]), int(bool(row["is_correction"])), nfc(row["rcept_no"]),
                    nfc(row["rcept_dt"]), nfc(row.get("flr_nm")), row.get("base_year"),
                    row.get("base_month"), manifest_rel, actual_rel, nfc(row["file_format"]),
                    int(row["n_files"]), json.dumps(source_files, ensure_ascii=False),
                )
                db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
                db.execute(
                    "INSERT INTO documents_fts VALUES (?,?,?,?,?,?,?,?)",
                    (values[0], values[2], values[3], values[9], values[8], values[13], values[5], values[6]),
                )
                inserted += 1

        if missing:
            raise RuntimeError("Manifest paths not found:\n" + "\n".join(missing[:20]))
        db.commit()
        checks = {
            "companies": db.execute("SELECT count(*) FROM companies").fetchone()[0],
            "documents": db.execute("SELECT count(*) FROM documents").fetchone()[0],
            "corrections": db.execute("SELECT count(*) FROM documents WHERE is_correction=1").fetchone()[0],
            "source_files": db.execute(
                "SELECT sum(json_array_length(source_files_json)) FROM documents"
            ).fetchone()[0],
        }
        if checks["companies"] != 70 or checks["documents"] != 4204 or checks["corrections"] != 1004:
            raise RuntimeError(f"Unexpected corpus counts: {checks}")
        print(json.dumps(checks, ensure_ascii=False))
    finally:
        db.close()

    temp.replace(output)
    print(f"METADATA_INDEX_OK {output} ({output.stat().st_size:,} bytes, {inserted} documents)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.corpus, args.output)


if __name__ == "__main__":
    main()
