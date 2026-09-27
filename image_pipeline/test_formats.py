"""Проверка извлечения картинок из разных форматов."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from PIL import Image

from image_pipeline.core import extract_images, supported_formats

ROOT = Path(__file__).resolve().parent / "_fmt_samples"


def _png_bytes(color=(40, 120, 90), size=(120, 120)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _make_image(path: Path, fmt: str, **save_kw) -> None:
    Image.new("RGB", (128, 128), (20, 90, 160)).save(path, fmt, **save_kw)


def _make_office(path: Path, media_name: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types></Types>")
        zf.writestr(media_name, _png_bytes())


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    samples: list[Path] = []

    for ext, fmt, kw in [
        (".png", "PNG", {}),
        (".jpg", "JPEG", {"quality": 90}),
        (".jpeg", "JPEG", {"quality": 90}),
        (".jfif", "JPEG", {"quality": 90}),
        (".webp", "WEBP", {}),
        (".gif", "GIF", {}),
        (".bmp", "BMP", {}),
        (".tif", "TIFF", {}),
    ]:
        p = ROOT / f"sample{ext}"
        _make_image(p, fmt, **kw)
        samples.append(p)

    # PDF via PyMuPDF
    import fitz

    pdf = ROOT / "sample.pdf"
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    page.insert_image(fitz.Rect(10, 10, 190, 190), stream=_png_bytes())
    doc.save(pdf)
    doc.close()
    samples.append(pdf)

    _make_office(ROOT / "sample.docx", "word/media/image1.png")
    _make_office(ROOT / "sample.pptx", "ppt/media/image1.png")
    _make_office(ROOT / "sample.xlsx", "xl/media/image1.png")
    _make_office(ROOT / "sample.odt", "Pictures/image1.png")
    samples.extend(
        [
            ROOT / "sample.docx",
            ROOT / "sample.pptx",
            ROOT / "sample.xlsx",
            ROOT / "sample.odt",
        ]
    )

    print("supported", supported_formats())
    failed = []
    for path in samples:
        try:
            blobs = extract_images(path)
            ok = len(blobs) >= 1
            print(f"{path.suffix:8} -> {len(blobs)} image(s) {'OK' if ok else 'EMPTY'}")
            if not ok:
                failed.append(path.name)
        except Exception as exc:
            print(f"{path.suffix:8} -> FAIL {exc}")
            failed.append(path.name)

    if failed:
        raise SystemExit(f"Failed: {failed}")
    print("ALL_FORMATS_OK")


if __name__ == "__main__":
    main()
