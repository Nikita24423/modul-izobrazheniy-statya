"""Вся логика модуля: извлечение, pHash, OCR, корпус, конвейер, отчёт, API."""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Optional

import imagehash
from PIL import Image, ImageOps

# --- модели -----------------------------------------------------------------

MIN_AREA = 4096
RASTER_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
SKIP_EXT = {".emf", ".wmf"}
MIN_OCR_CHARS = 12
DEFAULT_TEXT_TAU = 0.82
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "corpus.db"

_WIN_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    str(Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR" / "tesseract.exe"),
]
_USER_TESSDATA_PARENT = Path.home() / "AppData" / "Local" / "Tesseract-OCR"

_VIA = {"phash": "визуально (pHash)", "ocr": "по тексту (OCR)", "both": "визуально + OCR"}


@dataclass
class ImageBlob:
    data: bytes
    mime: str
    page: Optional[int] = None
    seq: int = 0
    width: int = 0
    height: int = 0
    sha256: str = ""


@dataclass
class CorpusEntry:
    phash64: int
    source_task_id: str
    source_document_id: str
    sha256: str = ""
    byte_size: int = 0
    thumb_path: str = ""
    ocr_text: str = ""


@dataclass
class ImageMatch:
    document_image_seq: int
    phash64: int
    hamming_distance: int
    source_task_id: str
    source_document_id: str
    low_confidence: bool = False
    thumb_path: str = ""
    match_via: str = "phash"
    text_similarity: float = 0.0
    source_ocr_text: str = ""


@dataclass
class DocumentImageRecord:
    seq: int
    phash64: int
    sha256: str
    width: int
    height: int
    thumb_path: str
    matches: list[ImageMatch] = field(default_factory=list)
    ocr_text: str = ""


@dataclass
class ImageCheckResult:
    task_id: str
    document_id: str
    document_path: str
    images: list[DocumentImageRecord] = field(default_factory=list)
    p_img_percent: float = 100.0
    n_total: int = 0
    n_match: int = 0
    tau: int = 10
    ocr_enabled: bool = False

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "document_id": self.document_id,
            "document_path": self.document_path,
            "p_img_percent": self.p_img_percent,
            "n_total": self.n_total,
            "n_match": self.n_match,
            "tau": self.tau,
            "ocr_enabled": self.ocr_enabled,
            "images": [
                {
                    "seq": img.seq,
                    "phash64": f"{img.phash64:016x}",
                    "sha256": img.sha256,
                    "width": img.width,
                    "height": img.height,
                    "thumb_path": img.thumb_path,
                    "ocr_text": img.ocr_text,
                    "matches": [
                        {
                            "hamming_distance": m.hamming_distance,
                            "source_task_id": m.source_task_id,
                            "source_document_id": m.source_document_id,
                            "low_confidence": m.low_confidence,
                            "match_via": m.match_via,
                            "text_similarity": m.text_similarity,
                        }
                        for m in img.matches
                    ],
                }
                for img in self.images
            ],
        }


# --- pHash ------------------------------------------------------------------


def compute_phash(blob: ImageBlob) -> int:
    with Image.open(io.BytesIO(blob.data)) as im:
        im = im.convert("RGB")
        h = imagehash.phash(im)
    return int(str(h), 16)


def phash_to_hex(value: int) -> str:
    return f"{value:016x}"


def phash_from_hex(hex_str: str) -> int:
    return int(hex_str, 16)


def hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


# --- извлечение -------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _probe_image(data: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


def _append_blob(
    blobs: list[ImageBlob],
    seen: set[str],
    data: bytes,
    mime: str,
    page: int | None,
    seq: int,
) -> int:
    if len(data) < 32:
        return seq
    digest = _sha256(data)
    if digest in seen:
        return seq
    size = _probe_image(data)
    if size is None:
        return seq
    w, h = size
    if w * h < MIN_AREA:
        return seq
    seen.add(digest)
    blobs.append(
        ImageBlob(
            data=data,
            mime=mime,
            page=page,
            seq=seq,
            width=w,
            height=h,
            sha256=digest,
        )
    )
    return seq + 1


def extract_from_pdf(path: Path) -> list[ImageBlob]:
    import fitz

    blobs: list[ImageBlob] = []
    seen: set[str] = set()
    seq = 0
    doc = fitz.open(path)
    try:
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            for img_info in page.get_images(full=True):
                xref = img_info[0]
                try:
                    extracted = doc.extract_image(xref)
                except Exception:
                    continue
                data = extracted.get("image")
                if not data:
                    continue
                ext = extracted.get("ext", "png")
                mime = f"image/{ext}" if ext != "jpg" else "image/jpeg"
                seq = _append_blob(blobs, seen, data, mime, page_idx + 1, seq)
    finally:
        doc.close()
    return blobs


def extract_from_docx(path: Path) -> list[ImageBlob]:
    blobs: list[ImageBlob] = []
    seen: set[str] = set()
    seq = 0
    with zipfile.ZipFile(path, "r") as zf:
        for name in sorted(zf.namelist()):
            if not name.startswith("word/media/"):
                continue
            ext = Path(name).suffix.lower()
            if ext in SKIP_EXT or ext not in RASTER_EXT:
                continue
            data = zf.read(name)
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".bmp": "image/bmp",
                ".tif": "image/tiff",
                ".tiff": "image/tiff",
                ".webp": "image/webp",
            }.get(ext, "application/octet-stream")
            seq = _append_blob(blobs, seen, data, mime, None, seq)
    return blobs


def extract_from_image(path: Path) -> list[ImageBlob]:
    data = path.read_bytes()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")
    blobs: list[ImageBlob] = []
    _append_blob(blobs, set(), data, mime, None, 0)
    return blobs


def extract_images(path: str | Path) -> list[ImageBlob]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return extract_from_pdf(p)
    if suffix == ".docx":
        return extract_from_docx(p)
    if suffix in RASTER_EXT:
        return extract_from_image(p)
    raise ValueError(
        f"Формат не поддерживается: {suffix} "
        f"(ожидается .pdf, .docx или растровое изображение)"
    )


# --- OCR --------------------------------------------------------------------


def _ensure_tessdata_prefix() -> None:
    user_td = _USER_TESSDATA_PARENT / "tessdata"
    if (user_td / "eng.traineddata").is_file() or (user_td / "rus.traineddata").is_file():
        os.environ.setdefault("TESSDATA_PREFIX", str(user_td))
        return
    pf = Path(r"C:\Program Files\Tesseract-OCR\tessdata")
    if (pf / "eng.traineddata").is_file():
        os.environ.setdefault("TESSDATA_PREFIX", str(pf))


def _configure_tesseract() -> str | None:
    try:
        import pytesseract
    except ImportError:
        return None

    _ensure_tessdata_prefix()
    which = shutil.which("tesseract")
    if which:
        pytesseract.pytesseract.tesseract_cmd = which
        return which

    env_cmd = os.environ.get("TESSERACT_CMD")
    if env_cmd and Path(env_cmd).is_file():
        pytesseract.pytesseract.tesseract_cmd = env_cmd
        return env_cmd

    for cand in _WIN_CANDIDATES:
        if Path(cand).is_file():
            pytesseract.pytesseract.tesseract_cmd = cand
            return cand

    cmd = getattr(pytesseract.pytesseract, "tesseract_cmd", None)
    if cmd and Path(str(cmd)).is_file():
        return str(cmd)
    return None


@lru_cache(maxsize=1)
def ocr_available() -> bool:
    try:
        import pytesseract
    except ImportError:
        return False
    if not _configure_tesseract():
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def normalize_ocr_text(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^\w\sа-яa-z0-9]+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def text_similarity(a: str, b: str) -> float:
    na, nb = normalize_ocr_text(a), normalize_ocr_text(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _prepare_for_ocr(im: Image.Image) -> Image.Image:
    im = im.convert("L")
    im = ImageOps.autocontrast(im)
    w, h = im.size
    if max(w, h) < 900:
        scale = 900 / max(w, h)
        im = im.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
    return im


def _langs() -> str:
    prefix = Path(os.environ.get("TESSDATA_PREFIX", "") or ".")
    candidates = [
        prefix,
        prefix / "tessdata",
        _USER_TESSDATA_PARENT / "tessdata",
        Path(r"C:\Program Files\Tesseract-OCR\tessdata"),
    ]
    for td in candidates:
        has_rus = (td / "rus.traineddata").is_file()
        has_eng = (td / "eng.traineddata").is_file()
        if has_rus and has_eng:
            return "rus+eng"
        if has_rus:
            return "rus"
    return "eng"


def extract_text(blob: ImageBlob, *, lang: str | None = None) -> str:
    if not ocr_available():
        return ""
    try:
        import pytesseract

        use_lang = lang or _langs()
        with Image.open(io.BytesIO(blob.data)) as raw:
            im = _prepare_for_ocr(raw)
            config = "--oem 3 --psm 6"
            try:
                text = pytesseract.image_to_string(im, lang=use_lang, config=config)
            except Exception:
                text = pytesseract.image_to_string(im, lang="eng", config=config)
        return normalize_ocr_text(text)
    except Exception:
        return ""


def ocr_status() -> dict:
    ok = ocr_available()
    cmd = None
    version = None
    if ok:
        try:
            import pytesseract

            cmd = str(pytesseract.pytesseract.tesseract_cmd)
            version = str(pytesseract.get_tesseract_version())
        except Exception:
            pass
    return {
        "available": ok,
        "tesseract_cmd": cmd,
        "version": version,
        "langs": _langs() if ok else None,
        "tessdata_prefix": os.environ.get("TESSDATA_PREFIX"),
    }


# --- matcher ----------------------------------------------------------------


def _better(a: ImageMatch, b: ImageMatch) -> bool:
    rank = {"both": 0, "phash": 1, "ocr": 2}
    if rank.get(a.match_via, 9) != rank.get(b.match_via, 9):
        return rank.get(a.match_via, 9) < rank.get(b.match_via, 9)
    if a.hamming_distance != b.hamming_distance:
        return a.hamming_distance < b.hamming_distance
    return a.text_similarity > b.text_similarity


def find_matches(
    phash64: int,
    blob: ImageBlob,
    corpus: list[CorpusEntry],
    *,
    tau: int = 10,
    low_confidence_above: int = 8,
    ocr_text: str = "",
    text_tau: float = DEFAULT_TEXT_TAU,
) -> list[ImageMatch]:
    by_doc: dict[str, ImageMatch] = {}

    for entry in corpus:
        d = hamming_distance(phash64, entry.phash64)
        visual = d <= tau
        if visual and d == 0 and entry.sha256 and blob.sha256 != entry.sha256:
            if entry.byte_size and len(blob.data) != entry.byte_size:
                visual = False

        sim = 0.0
        textual = False
        if (
            ocr_text
            and entry.ocr_text
            and len(ocr_text) >= MIN_OCR_CHARS
            and len(entry.ocr_text) >= MIN_OCR_CHARS
        ):
            sim = text_similarity(ocr_text, entry.ocr_text)
            textual = sim >= text_tau

        if not visual and not textual:
            continue

        if visual and textual:
            via = "both"
        elif visual:
            via = "phash"
        else:
            via = "ocr"

        hit = ImageMatch(
            document_image_seq=blob.seq,
            phash64=phash64,
            hamming_distance=d if visual else 64,
            source_task_id=entry.source_task_id,
            source_document_id=entry.source_document_id,
            low_confidence=(via == "phash" and low_confidence_above < d <= tau)
            or (via == "ocr" and sim < 0.92),
            thumb_path=entry.thumb_path,
            match_via=via,
            text_similarity=round(sim, 3),
            source_ocr_text=entry.ocr_text[:500],
        )

        prev = by_doc.get(entry.source_document_id)
        if prev is None or _better(hit, prev):
            by_doc[entry.source_document_id] = hit

    hits = list(by_doc.values())
    hits.sort(
        key=lambda m: (
            0 if m.match_via == "both" else 1 if m.match_via == "phash" else 2,
            m.hamming_distance,
            -m.text_similarity,
        )
    )
    return hits


# --- SQLite корпус ----------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS corpus_image_hash (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phash64 TEXT NOT NULL,
    source_task_id TEXT NOT NULL,
    source_document_id TEXT NOT NULL,
    sha256 TEXT DEFAULT '',
    byte_size INTEGER DEFAULT 0,
    thumb_path TEXT DEFAULT '',
    ocr_text TEXT DEFAULT '',
    indexed_at TEXT DEFAULT (datetime('now')),
    UNIQUE (phash64, source_document_id)
);
CREATE INDEX IF NOT EXISTS idx_corpus_phash ON corpus_image_hash(phash64);
"""


class CorpusStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(corpus_image_hash)").fetchall()}
        if "ocr_text" not in cols:
            conn.execute("ALTER TABLE corpus_image_hash ADD COLUMN ocr_text TEXT DEFAULT ''")

    def add_entry(self, entry: CorpusEntry) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO corpus_image_hash
                (phash64, source_task_id, source_document_id, sha256, byte_size, thumb_path, ocr_text)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    phash_to_hex(entry.phash64),
                    entry.source_task_id,
                    entry.source_document_id,
                    entry.sha256,
                    entry.byte_size,
                    entry.thumb_path,
                    entry.ocr_text or "",
                ),
            )

    def iter_entries(self, exclude_task_id: str | None = None) -> list[CorpusEntry]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if exclude_task_id:
                rows = conn.execute(
                    """
                    SELECT phash64, source_task_id, source_document_id, sha256, byte_size, thumb_path, ocr_text
                    FROM corpus_image_hash WHERE source_task_id != ?
                    """,
                    (exclude_task_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT phash64, source_task_id, source_document_id, sha256, byte_size, thumb_path, ocr_text
                    FROM corpus_image_hash
                    """
                ).fetchall()
        return [
            CorpusEntry(
                phash64=phash_from_hex(r["phash64"]),
                source_task_id=r["source_task_id"],
                source_document_id=r["source_document_id"],
                sha256=r["sha256"] or "",
                byte_size=r["byte_size"] or 0,
                thumb_path=r["thumb_path"] or "",
                ocr_text=r["ocr_text"] or "",
            )
            for r in rows
        ]

    def count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM corpus_image_hash").fetchone()
        return int(row[0]) if row else 0


# --- конвейер ---------------------------------------------------------------


def _save_thumb(data: bytes, out_dir: Path, seq: int) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"img_{seq:03d}.jpg"
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        im.thumbnail((200, 200))
        im.save(path, "JPEG", quality=85)
    return str(path)


def process_document(
    path: str | Path,
    *,
    task_id: str,
    document_id: str,
    store: CorpusStore,
    tau: int = 10,
    thumb_dir: Path | None = None,
    index_to_corpus: bool = True,
    use_ocr: bool = True,
    text_tau: float = 0.82,
) -> ImageCheckResult:
    p = Path(path)
    thumb_root = thumb_dir or (store.db_path.parent / "thumbs" / document_id)
    corpus = store.iter_entries(exclude_task_id=task_id)
    ocr_on = use_ocr and ocr_available()

    records: list[DocumentImageRecord] = []
    n_match = 0

    for blob in extract_images(p):
        phash64 = compute_phash(blob)
        thumb = _save_thumb(blob.data, thumb_root, blob.seq)
        ocr_text = extract_text(blob) if ocr_on else ""
        matches = find_matches(
            phash64, blob, corpus, tau=tau, ocr_text=ocr_text, text_tau=text_tau
        )
        if matches:
            n_match += 1
        records.append(
            DocumentImageRecord(
                seq=blob.seq,
                phash64=phash64,
                sha256=blob.sha256,
                width=blob.width,
                height=blob.height,
                thumb_path=thumb,
                matches=matches,
                ocr_text=ocr_text,
            )
        )
        if index_to_corpus:
            store.add_entry(
                CorpusEntry(
                    phash64=phash64,
                    source_task_id=task_id,
                    source_document_id=document_id,
                    sha256=blob.sha256,
                    byte_size=len(blob.data),
                    thumb_path=thumb,
                    ocr_text=ocr_text,
                )
            )

    n_total = len(records)
    p_img = 0.0 if n_total == 0 else (n_match / n_total) * 100.0
    return ImageCheckResult(
        task_id=task_id,
        document_id=document_id,
        document_path=str(p.resolve()),
        images=records,
        p_img_percent=round(p_img, 1),
        n_total=n_total,
        n_match=n_match,
        tau=tau,
        ocr_enabled=ocr_on,
    )


def index_document(
    path: str | Path,
    *,
    task_id: str,
    document_id: str,
    store: CorpusStore,
    thumb_dir: Path | None = None,
    use_ocr: bool = True,
) -> int:
    p = Path(path)
    thumb_root = thumb_dir or (store.db_path.parent / "thumbs" / document_id)
    ocr_on = use_ocr and ocr_available()
    count = 0
    for blob in extract_images(p):
        phash64 = compute_phash(blob)
        thumb = _save_thumb(blob.data, thumb_root, blob.seq)
        ocr_text = extract_text(blob) if ocr_on else ""
        store.add_entry(
            CorpusEntry(
                phash64=phash64,
                source_task_id=task_id,
                source_document_id=document_id,
                sha256=blob.sha256,
                byte_size=len(blob.data),
                thumb_path=thumb,
                ocr_text=ocr_text,
            )
        )
        count += 1
    return count


# --- HTML-отчёт -------------------------------------------------------------


def render_html(result: ImageCheckResult) -> str:
    rows = []
    for img in result.images:
        if img.matches:
            best = img.matches[0]
            status = "заимствование"
            if best.low_confidence:
                status += " (низкая уверенность)"
            via = _VIA.get(best.match_via, best.match_via)
            match_cell = f"{via}; источник: {escape(best.source_document_id)}"
            if best.match_via in ("phash", "both") and best.hamming_distance <= 63:
                match_cell += f"; d_H = {best.hamming_distance}"
            if best.text_similarity:
                match_cell += f"; сходство текста = {best.text_similarity:.2f}"
        else:
            status = "оригинал (похожих не найдено)"
            match_cell = "—"

        ocr_cell = escape(img.ocr_text[:200]) if img.ocr_text else "—"
        thumb = escape(img.thumb_path)
        rows.append(
            f"<tr>"
            f'<td><img src="{thumb}" width="120" alt="#{img.seq}"></td>'
            f"<td>{img.seq}</td>"
            f"<td>{img.width}×{img.height}</td>"
            f"<td>{status}</td>"
            f"<td>{match_cell}</td>"
            f"<td><small>{ocr_cell}</small></td>"
            f"</tr>"
        )

    body_rows = "\n".join(rows) if rows else '<tr><td colspan="6">Изображений не извлечено</td></tr>'
    ocr_flag = "вкл." if result.ocr_enabled else "выкл. (установите Tesseract OCR)"

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Иллюстрации — {escape(result.document_id)}</title>
  <style>
    body {{ font-family: Times New Roman, serif; margin: 2em; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #333; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #143A5C; color: #fff; }}
    .summary {{ margin-bottom: 1.5em; }}
  </style>
</head>
<body>
  <h1>Блок «Иллюстрации»</h1>
  <div class="summary">
    <p><b>Документ:</b> {escape(result.document_path)}</p>
    <p><b>Доля заимствованных иллюстраций P_img:</b> {result.p_img_percent} %</p>
    <p><b>Извлечено:</b> {result.n_total}; <b>с совпадениями:</b> {result.n_match}</p>
    <p><b>Порог τ:</b> {result.tau}; <b>OCR:</b> {ocr_flag}</p>
  </div>
  <table>
    <thead>
      <tr>
        <th>Миниатюра</th><th>№</th><th>Размер</th>
        <th>Статус</th><th>Лучшее совпадение</th><th>Текст с картинки (OCR)</th>
      </tr>
    </thead>
    <tbody>
      {body_rows}
    </tbody>
  </table>
</body>
</html>
"""


def write_report(result: ImageCheckResult, out_path: str | Path) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(result), encoding="utf-8")
    return path


# --- API для worker ---------------------------------------------------------


class ImageBorrowingService:
    """
    Сервис проверки иллюстраций.

        svc = ImageBorrowingService()
        svc.add_to_corpus("methodichka.pdf", task_id="1", document_id="ref-01")
        result = svc.check("student.docx", task_id="2", document_id="stu-42")
    """

    def __init__(self, db_path: str | Path | None = None, tau: int = 10):
        self.db_path = Path(db_path or DEFAULT_DB)
        self.tau = tau
        self._store = CorpusStore(self.db_path)

    @property
    def store(self) -> CorpusStore:
        return self._store

    def corpus_size(self) -> int:
        return self._store.count()

    def add_to_corpus(
        self,
        path: str | Path,
        *,
        task_id: str,
        document_id: str,
        use_ocr: bool = True,
    ) -> int:
        return index_document(
            path,
            task_id=task_id,
            document_id=document_id,
            store=self._store,
            use_ocr=use_ocr,
        )

    def check(
        self,
        path: str | Path,
        *,
        task_id: str,
        document_id: str,
        index_to_corpus: bool = False,
        tau: int | None = None,
        use_ocr: bool = True,
        text_tau: float = 0.82,
    ) -> ImageCheckResult:
        return process_document(
            path,
            task_id=task_id,
            document_id=document_id,
            store=self._store,
            tau=tau if tau is not None else self.tau,
            index_to_corpus=index_to_corpus,
            use_ocr=use_ocr,
            text_tau=text_tau,
        )

    def check_and_report(
        self,
        path: str | Path,
        report_path: str | Path,
        *,
        task_id: str,
        document_id: str,
        index_to_corpus: bool = False,
    ) -> ImageCheckResult:
        result = self.check(
            path,
            task_id=task_id,
            document_id=document_id,
            index_to_corpus=index_to_corpus,
        )
        write_report(result, report_path)
        return result
