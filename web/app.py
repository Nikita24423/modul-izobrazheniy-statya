"""
Веб-интерфейс модуля иллюстраций.

Запуск из корня проекта:
    uvicorn web.app:app --reload --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from image_pipeline import DEFAULT_DB, ImageBorrowingService, ImageCheckResult, ocr_available

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
JOBS_DIR = DATA_DIR / "jobs"
DB_PATH = Path(DEFAULT_DB)

ALLOWED_EXT = {".pdf", ".docx", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
VIA_LABELS = {
    "phash": "Совпадение по виду картинки",
    "ocr": "Совпадение по тексту на картинке",
    "both": "Совпадение и по виду, и по тексту",
}

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "thumbs").mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Проверка иллюстраций", version="1.0.0")
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
app.mount("/thumbs", StaticFiles(directory=str(DATA_DIR / "thumbs")), name="thumbs")
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.globals["via_label"] = lambda key: VIA_LABELS.get(key, key)


def _svc(tau: int = 10) -> ImageBorrowingService:
    return ImageBorrowingService(db_path=DB_PATH, tau=tau)


def _safe_name(name: str) -> str:
    base = Path(name).name
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in base) or "file"


def _thumb_url(path: str) -> str:
    if not path:
        return ""
    p = Path(path)
    try:
        rel = p.resolve().relative_to((DATA_DIR / "thumbs").resolve())
        return f"/thumbs/{rel.as_posix()}"
    except ValueError:
        return ""


def _result_to_view(result: ImageCheckResult) -> dict:
    images = []
    for img in result.images:
        best = img.matches[0] if img.matches else None
        images.append(
            {
                "seq": img.seq,
                "width": img.width,
                "height": img.height,
                "thumb_url": _thumb_url(img.thumb_path),
                "ocr_text": img.ocr_text,
                "matched": bool(img.matches),
                "low_confidence": bool(best and best.low_confidence),
                "match_via": best.match_via if best else "",
                "hamming_distance": best.hamming_distance if best else None,
                "text_similarity": best.text_similarity if best else 0.0,
                "source_document_id": best.source_document_id if best else "",
                "source_task_id": best.source_task_id if best else "",
                "source_thumb_url": _thumb_url(best.thumb_path) if best else "",
            }
        )
    return {
        "task_id": result.task_id,
        "document_id": result.document_id,
        "document_path": result.document_path,
        "p_img_percent": result.p_img_percent,
        "n_total": result.n_total,
        "n_match": result.n_match,
        "tau": result.tau,
        "ocr_enabled": result.ocr_enabled,
        "images": images,
    }


def _save_job(job_id: str, payload: dict) -> None:
    (JOBS_DIR / f"{job_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_job(job_id: str) -> dict:
    path = JOBS_DIR / f"{job_id}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Результат не найден")
    return json.loads(path.read_text(encoding="utf-8"))


def _merge_results(parts: list[ImageCheckResult], *, task_id: str, document_id: str) -> ImageCheckResult:
    images = []
    seq = 0
    for part in parts:
        for img in part.images:
            img.seq = seq
            for m in img.matches:
                m.document_image_seq = seq
            images.append(img)
            seq += 1
    n_total = len(images)
    n_match = sum(1 for img in images if img.matches)
    p_img = 100.0 if n_total == 0 else round(100.0 * n_match / n_total, 2)
    names = ", ".join(Path(p.document_path).name for p in parts)
    return ImageCheckResult(
        task_id=task_id,
        document_id=document_id,
        document_path=names,
        images=images,
        p_img_percent=p_img,
        n_total=n_total,
        n_match=n_match,
        tau=parts[0].tau if parts else 10,
        ocr_enabled=any(p.ocr_enabled for p in parts),
    )


@app.get("/", response_class=HTMLResponse)
async def upload_page(request: Request):
    svc = _svc()
    size = svc.corpus_size()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "corpus_size": size,
            "library_size": size,
            "ocr_available": ocr_available(),
        },
    )


@app.post("/check")
async def check_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    mode: str = Form("check"),
    task_id: str = Form(""),
    document_id: str = Form(""),
    tau: int = Form(10),
    index_to_corpus: bool = Form(False),
):
    if not files or all(not f.filename for f in files):
        raise HTTPException(status_code=400, detail="Выберите хотя бы один файл")

    job_id = uuid.uuid4().hex[:12]
    task_id = (task_id or f"web-{job_id}").strip()
    document_id = (document_id or f"doc-{job_id}").strip()
    tau = max(0, min(int(tau), 32))

    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    saved: list[Path] = []
    try:
        for upload in files:
            if not upload.filename:
                continue
            suffix = Path(upload.filename).suffix.lower()
            if suffix not in ALLOWED_EXT:
                raise HTTPException(
                    status_code=400,
                    detail=f"Формат не поддерживается: {suffix}",
                )
            dest = job_dir / _safe_name(upload.filename)
            with dest.open("wb") as out:
                shutil.copyfileobj(upload.file, out)
            saved.append(dest)

        if not saved:
            raise HTTPException(status_code=400, detail="Пустая загрузка")

        svc = _svc(tau=tau)

        if mode == "index":
            total = 0
            for path in saved:
                total += svc.add_to_corpus(
                    path,
                    task_id=task_id,
                    document_id=f"{document_id}-{path.stem}" if len(saved) > 1 else document_id,
                )
            _save_job(
                job_id,
                {
                    "kind": "index",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "task_id": task_id,
                    "document_id": document_id,
                    "indexed": total,
                    "corpus_size": svc.corpus_size(),
                    "files": [p.name for p in saved],
                },
            )
            return RedirectResponse(url=f"/results/{job_id}", status_code=303)

        parts: list[ImageCheckResult] = []
        for i, path in enumerate(saved):
            doc_id = document_id if len(saved) == 1 else f"{document_id}-{i + 1}"
            parts.append(
                svc.check(
                    path,
                    task_id=task_id,
                    document_id=doc_id,
                    index_to_corpus=index_to_corpus,
                    tau=tau,
                )
            )

        result = parts[0] if len(parts) == 1 else _merge_results(parts, task_id=task_id, document_id=document_id)
        view = _result_to_view(result)
        view.update(
            {
                "kind": "check",
                "job_id": job_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "files": [p.name for p in saved],
                "corpus_size": svc.corpus_size(),
            }
        )
        _save_job(job_id, view)
        return RedirectResponse(url=f"/results/{job_id}", status_code=303)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/results/{job_id}", response_class=HTMLResponse)
async def results_page(request: Request, job_id: str):
    job = _load_job(job_id)
    template = "results_index.html" if job.get("kind") == "index" else "results.html"
    return templates.TemplateResponse(request, template, {"job": job, "job_id": job_id})


@app.get("/api/corpus")
async def corpus_info():
    svc = _svc()
    return {"corpus_size": svc.corpus_size(), "ocr_available": ocr_available()}
