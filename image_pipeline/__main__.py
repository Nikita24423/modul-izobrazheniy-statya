"""Запуск: python -m image_pipeline index … | check … | info"""
from __future__ import annotations

import argparse
import json
import sys

from .core import DEFAULT_DB, ImageBorrowingService, write_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="image_pipeline",
        description="Обнаружение заимствований изображений (pHash + OCR + корпус)",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Путь к SQLite корпуса")
    parser.add_argument("--tau", type=int, default=10, help="Порог Хэмминга")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="Добавить документ в корпус")
    p_index.add_argument("file", help="PDF или DOCX")
    p_index.add_argument("--task-id", required=True)
    p_index.add_argument("--document-id", required=True)

    p_check = sub.add_parser("check", help="Проверить документ")
    p_check.add_argument("file", help="PDF или DOCX")
    p_check.add_argument("--task-id", required=True)
    p_check.add_argument("--document-id", required=True)
    p_check.add_argument("--report", help="Путь к HTML-отчёту")
    p_check.add_argument("--index", action="store_true", help="Добавить в корпус после проверки")

    sub.add_parser("info", help="Размер корпуса")
    args = parser.parse_args(argv)

    svc = ImageBorrowingService(db_path=args.db, tau=args.tau)

    if args.command == "info":
        print(f"Записей в корпусе: {svc.corpus_size()}")
        return 0

    if args.command == "index":
        n = svc.add_to_corpus(args.file, task_id=args.task_id, document_id=args.document_id)
        print(f"Проиндексировано: {n}; корпус: {svc.corpus_size()}")
        return 0

    if args.command == "check":
        result = svc.check(
            args.file,
            task_id=args.task_id,
            document_id=args.document_id,
            index_to_corpus=args.index,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        if args.report:
            out = write_report(result, args.report)
            print(f"HTML: {out}", file=sys.stderr)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
