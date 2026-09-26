"""
# Проверка иллюстраций

Модуль обнаружения заимствованных изображений (pHash + OCR) с веб-интерфейсом.

## Установка

```bash
pip install -r image_pipeline/requirements.txt
```

Опционально: [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) для текстового сравнения.

## CLI

```bash
python -m image_pipeline index file.pdf --task-id 1 --document-id ref
python -m image_pipeline check file.docx --task-id 2 --document-id stu --report out.html
```

## Веб-интерфейс

Из корня проекта:

```bash
uvicorn web.app:app --reload --host 127.0.0.1 --port 8000
```

Откройте http://127.0.0.1:8000 — загрузка файлов и страница результатов.

Поддерживаются: PNG, JPG, WEBP, GIF, BMP, TIFF, PDF, DOCX.

## Деплой на Render (бесплатно)

Рекомендуемый хостинг для этого проекта — [Render](https://render.com) (Free Web Service).

1. Репозиторий уже содержит `Dockerfile` и `render.yaml`.
2. Откройте Blueprint-деплой (после пуша в GitHub):
   https://render.com/deploy?repo=https://github.com/Nikita24423/modul-izobrazheniy-statya
3. Выберите план **Free** и подтвердите создание сервиса.

Ограничения Free: сервис «засыпает» без трафика ~15 мин; диск эфемерный — корпус (`data/corpus.db`) сбрасывается при рестарте.
"""
