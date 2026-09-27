"""
Веб-интерфейс модуля иллюстраций.

Запуск из корня проекта:
    uvicorn web.app:app --reload --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from image_pipeline import ImageBorrowingService, ImageCheckResult, ocr_available
from image_pipeline.core import RASTER_EXT, SUPPORTED_DOC_EXT, supported_formats
from web.storage import PersistentStore, storage_info

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"

ALLOWED_EXT = SUPPORTED_DOC_EXT | RASTER_EXT
VIA_LABELS = {
    "phash": "Совпадение по виду картинки",
    "ocr": "Совпадение по тексту на картинке",
    "both": "Совпадение и по виду, и по тексту",
}

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "thumbs").mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Проверка иллюстраций", version="1.1.0")
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.globals["via_label"] = lambda key: VIA_LABELS.get(key, key)

_store: PersistentStore | None = None


def get_store() -> PersistentStore:
    global _store
    if _store is None:
        _store = PersistentStore()
    return _store


def _svc(tau: int = 10) -> ImageBorrowingService:
    store = get_store()
    return ImageBorrowingService(db_path=store.db_path, tau=tau, store=store)


def _safe_name(name: str) -> str:
    base = Path(name).name
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in base) or "file"


def _thumb_url(path: str) -> str:
    if not path:
        return ""
    if path.startswith("db:"):
        return f"/media/thumb/{path[3:]}"
    p = Path(path)
    try:
        rel = p.resolve().relative_to((DATA_DIR / "thumbs").resolve())
        return f"/media/file/{rel.as_posix()}"
    except ValueError:
        name = p.name
        parent = p.parent.name
        return f"/media/thumb/{parent}/{name}"


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
    get_store().save_job(job_id, payload)


def _load_job(job_id: str) -> dict:
    job = get_store().load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Результат не найден")
    return job


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


@app.on_event("startup")
def _startup() -> None:
    get_store()


@app.get("/", response_class=HTMLResponse)
async def upload_page(request: Request):
    svc = _svc()
    size = svc.corpus_size()
    info = storage_info()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "corpus_size": size,
            "library_size": size,
            "ocr_available": ocr_available(),
            "storage": info,
            "formats": supported_formats(),
            "accept_attr": ",".join(sorted(ALLOWED_EXT)) + ",image/*",
            "documents": get_store().list_documents(),
        },
    )


def _save_uploads(uploads: list[UploadFile], dest_dir: Path) -> list[Path]:
    saved: list[Path] = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    for upload in uploads:
        if not upload.filename:
            continue
        suffix = Path(upload.filename).suffix.lower()
        if suffix not in ALLOWED_EXT:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Формат не поддерживается: {suffix}. "
                    f"Можно: фото ({', '.join(sorted(RASTER_EXT))}), "
                    f"документы ({', '.join(sorted(SUPPORTED_DOC_EXT))})"
                ),
            )
        dest = dest_dir / _safe_name(upload.filename)
        # избегаем перезаписи одинаковых имён
        if dest.exists():
            dest = dest_dir / f"{dest.stem}_{uuid.uuid4().hex[:6]}{dest.suffix}"
        with dest.open("wb") as out:
            shutil.copyfileobj(upload.file, out)
        saved.append(dest)
    return saved


@app.post("/check")
async def check_upload(
    files: list[UploadFile] | None = File(None),
    references: list[UploadFile] | None = File(None),
    index_files: list[UploadFile] | None = File(None),
    library_docs: list[str] = Form(default=[]),
    mode: str = Form("check"),
    task_id: str = Form(""),
    document_id: str = Form(""),
    tau: int = Form(10),
    index_to_corpus: bool = Form(False),
):
    files = files or []
    references = references or []
    index_files = index_files or []
    # Form(list) sometimes приходит строкой
    if isinstance(library_docs, str):
        library_docs = [library_docs] if library_docs else []
    library_docs = [d.strip() for d in library_docs if d and d.strip()]

    job_id = uuid.uuid4().hex[:12]
    task_id = (task_id or f"web-{job_id}").strip()
    document_id = (document_id or f"doc-{job_id}").strip()
    tau = max(0, min(int(tau), 32))

    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        svc = _svc(tau=tau)

        if mode == "index":
            saved = _save_uploads(index_files or files, job_dir / "index")
            if not saved:
                raise HTTPException(status_code=400, detail="Выберите файлы для базы эталонов")
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

        query_paths = _save_uploads(files, job_dir / "query")
        ref_paths = _save_uploads(references, job_dir / "refs")
        if not query_paths:
            raise HTTPException(status_code=400, detail="Загрузите изображение для проверки")
        if not ref_paths and not library_docs:
            raise HTTPException(
                status_code=400,
                detail="Выберите эталоны из базы или загрузите хотя бы один новый",
            )

        include_ids: list[str] = list(library_docs)
        ref_task_id = f"ref-{job_id}"
        uploaded_ref_docs: list[str] = []
        for i, path in enumerate(ref_paths):
            doc_ref = f"etalon-{i + 1}-{path.stem}"
            uploaded_ref_docs.append(doc_ref)
            include_ids.append(doc_ref)
            svc.add_to_corpus(
                path,
                task_id=ref_task_id,
                document_id=doc_ref,
            )

        check_task_id = f"check-{job_id}"
        parts: list[ImageCheckResult] = []
        for i, path in enumerate(query_paths):
            doc_id = document_id if len(query_paths) == 1 else f"{document_id}-{i + 1}"
            parts.append(
                svc.check(
                    path,
                    task_id=check_task_id,
                    document_id=doc_id,
                    index_to_corpus=False,
                    tau=tau,
                    include_document_ids=include_ids,
                )
            )

        if not index_to_corpus:
            for doc in uploaded_ref_docs:
                get_store().delete_document(doc)

        result = (
            parts[0]
            if len(parts) == 1
            else _merge_results(parts, task_id=check_task_id, document_id=document_id)
        )
        view = _result_to_view(result)
        view.update(
            {
                "kind": "check",
                "job_id": job_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "files": [p.name for p in query_paths],
                "reference_files": [p.name for p in ref_paths],
                "library_docs": library_docs,
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
    store = get_store()
    template = "results_index.html" if job.get("kind") == "index" else "results.html"
    return templates.TemplateResponse(
        request,
        template,
        {
            "job": job,
            "job_id": job_id,
            "library_size": store.count(),
            "storage": storage_info(),
        },
    )


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request, doc: str = ""):
    store = get_store()
    documents = store.list_documents()
    selected = doc.strip()
    if selected and not any(d["document_id"] == selected for d in documents):
        selected = ""
    items = store.list_library(document_id=selected or None)
    for item in items:
        item["thumb_url"] = _thumb_url(item.get("thumb_path") or "")
        item["label"] = f"№{item['id']} · {item['source_document_id']}"
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "documents": documents,
            "items": items,
            "selected_doc": selected,
            "library_size": store.count(),
            "storage": storage_info(),
            "message": request.query_params.get("msg", ""),
        },
    )


@app.post("/library/delete")
async def library_delete(
    entry_id: int = Form(0),
    document_id: str = Form(""),
    delete_scope: str = Form("one"),
):
    store = get_store()
    if delete_scope == "document":
        doc = document_id.strip()
        if not doc:
            raise HTTPException(status_code=400, detail="Выберите набор в списке")
        n = store.delete_document(doc)
        return RedirectResponse(
            url=f"/library?msg=Удалено картинок из набора: {n}",
            status_code=303,
        )
    if entry_id <= 0:
        raise HTTPException(status_code=400, detail="Выберите картинку в списке")
    ok = store.delete_entry(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Картинка не найдена")
    return RedirectResponse(
        url="/library?msg=Картинка удалена из базы эталонов",
        status_code=303,
    )


@app.get("/media/thumb/{thumb_key:path}")
async def media_thumb(thumb_key: str):
    data = get_store().get_thumb(thumb_key)
    if not data:
        # fallback: файл на диске
        path = DATA_DIR / "thumbs" / thumb_key
        if path.is_file():
            return Response(path.read_bytes(), media_type="image/jpeg")
        raise HTTPException(status_code=404, detail="Миниатюра не найдена")
    return Response(data, media_type="image/jpeg")


@app.get("/media/file/{file_path:path}")
async def media_file(file_path: str):
    path = (DATA_DIR / "thumbs" / file_path).resolve()
    root = (DATA_DIR / "thumbs").resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return Response(path.read_bytes(), media_type="image/jpeg")


@app.get("/api/corpus")
async def corpus_info():
    svc = _svc()
    return {
        "corpus_size": svc.corpus_size(),
        "ocr_available": ocr_available(),
        "storage": storage_info(),
    }
