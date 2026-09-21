FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-ell \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./document_processor_service/requirements.txt

RUN pip install --no-cache-dir -r document_processor_service/requirements.txt

COPY __init__.py ./document_processor_service/__init__.py
COPY app ./document_processor_service/app

EXPOSE 8000

CMD ["uvicorn", "document_processor_service.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
