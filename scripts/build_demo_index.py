#!/usr/bin/env python3
"""Build a tiny synthetic index through the original schema and text pipeline."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_metadata_index import create_schema as metadata_schema
from scripts.build_content_index import create_schema as content_schema, extract_lines, make_chunks


def build(output: Path) -> None:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Build alongside the destination so an interrupted run does not damage it.
    temporary = output.with_suffix(output.suffix + '.building')
    if temporary.exists():
        raise FileExistsError(f'Remove the incomplete demo build before retrying: {temporary}')
    with sqlite3.connect(temporary) as db:
        metadata_schema(db)
        content_schema(db)
        for slug, company in [('alpha', '가상알파'), ('beta', '가상베타')]:
            code, stock = f'DEMO-{slug}', f'DEMO-{slug.upper()}'
            source = f'examples/demo/{slug}.xml'
            doc_id, receipt = f'demo-{slug}-2025', f'DEMO-{slug.upper()}-2025'
            report = '사업보고서 (2025.12) [가상 데이터]'
            db.execute('INSERT INTO companies VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (code, stock, company, company, f'Fictional {slug}', 'DEMO',
                        'DEMO', 0, 'DEMO', None, '12', 0))
            db.execute('INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (doc_id, code, company, company, stock, 'DEMO', 'DEMO',
                        'periodic', 'annual', report, 0, receipt, '20260301',
                        company, 2025, 12, source, source, 'xml', 1,
                        json.dumps([source])))
            db.execute('INSERT INTO documents_fts VALUES (?,?,?,?,?,?,?,?)',
                       (doc_id, company, company, report, 'annual', company, 'DEMO', 'DEMO'))
            chunks = make_chunks(extract_lines(ROOT / source), target_chars=2200, overlap_lines=2)
            for index, text in enumerate(chunks):
                cur = db.execute('INSERT INTO chunks(doc_id,source_file,chunk_no,text,search_meta) VALUES (?,?,?,?,?)',
                                 (doc_id, source, index, text, company))
                db.execute('INSERT INTO chunks_fts(rowid,text,search_meta) VALUES (?,?,?)',
                           (cur.lastrowid, text, company))
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Synthetic demo index failed its integrity check')
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    db.close()
    temporary.replace(output)
    print(f'SYNTHETIC_DEMO_INDEX_OK {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/index/demo.db')
    build(parser.parse_args().output)
