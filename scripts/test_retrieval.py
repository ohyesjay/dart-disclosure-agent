#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.retriever import Retriever


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--db", default=str(ROOT / "data/index/search.db"))
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    retriever = Retriever(args.db)
    result = retriever.search(args.question, limit=args.limit)
    print(json.dumps({k: v for k, v in result.items() if k != "hits"}, ensure_ascii=False, indent=2))
    print("\n" + retriever.format_context(result))


if __name__ == "__main__":
    main()
