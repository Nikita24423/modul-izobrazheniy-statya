"""Демо: два похожих PNG в DOCX → совпадение в корпусе.

Запуск из корня проекта:
    python -m image_pipeline.demo
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

from .core import CorpusStore, index_document, process_document


def _make_png(color: tuple[int, int, int], label: str) -> bytes:
    im = Image.new("RGB", (128, 128), color)
    draw = ImageDraw.Draw(im)
    draw.text((10, 55), label, fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _make_docx_with_image(png: bytes, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>Test</w:t></w:r></w:p></w:body>
</w:document>"""

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/media/image1.png", png)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    demo = root / "data" / "demo"
    db = demo / "corpus.db"

    ref_png = _make_png((40, 90, 160), "REF")
    query_png = _make_png((40, 90, 160), "COPY")

    ref_doc = demo / "reference.docx"
    query_doc = demo / "query.docx"
    _make_docx_with_image(ref_png, ref_doc)
    _make_docx_with_image(query_png, query_doc)

    if db.exists():
        db.unlink()

    store = CorpusStore(db)
    index_document(ref_doc, task_id="1", document_id="ref-demo", store=store)

    result = process_document(
        query_doc,
        task_id="2",
        document_id="query-demo",
        store=store,
        index_to_corpus=False,
    )

    print(f"Извлечено: {result.n_total}")
    print(f"P_img: {result.p_img_percent} %")
    print(f"Совпадений: {result.n_match}")
    print(f"OCR: {'вкл.' if result.ocr_enabled else 'выкл.'}")
    if result.images and result.images[0].matches:
        m = result.images[0].matches[0]
        print(
            f"Лучшее: via={m.match_via}, d_H={m.hamming_distance}, "
            f"text_sim={m.text_similarity} (источник {m.source_document_id})"
        )
        if result.images[0].ocr_text:
            print(f"OCR текст: {result.images[0].ocr_text[:120]}")
    else:
        raise SystemExit("Ожидалось совпадение — проверьте демо")


if __name__ == "__main__":
    main()
