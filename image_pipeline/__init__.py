"""
image_pipeline — модуль обнаружения заимствований изображений.

    from image_pipeline import ImageBorrowingService

    svc = ImageBorrowingService(db_path="data/corpus.db")
    svc.add_to_corpus("etalon.pdf", task_id="1", document_id="ref")
    r = svc.check("student.docx", task_id="2", document_id="stu")
    print(r.p_img_percent)
"""

from .core import (
    DEFAULT_DB,
    RASTER_EXT,
    SUPPORTED_DOC_EXT,
    CorpusEntry,
    CorpusStore,
    DocumentImageRecord,
    ImageBlob,
    ImageBorrowingService,
    ImageCheckResult,
    ImageMatch,
    compute_phash,
    extract_images,
    extract_text,
    hamming_distance,
    index_document,
    ocr_available,
    phash_from_hex,
    phash_to_hex,
    process_document,
    render_html,
    supported_formats,
    text_similarity,
    write_report,
)

__all__ = [
    "ImageBorrowingService",
    "ImageCheckResult",
    "ImageBlob",
    "ImageMatch",
    "CorpusEntry",
    "DocumentImageRecord",
    "CorpusStore",
    "DEFAULT_DB",
    "RASTER_EXT",
    "SUPPORTED_DOC_EXT",
    "extract_images",
    "compute_phash",
    "hamming_distance",
    "phash_to_hex",
    "phash_from_hex",
    "extract_text",
    "ocr_available",
    "text_similarity",
    "index_document",
    "process_document",
    "render_html",
    "write_report",
    "supported_formats",
]

__version__ = "1.3.0"
