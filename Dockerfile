FROM python:3.11-slim

# LibreOffice + Hebrew fonts, needed to convert the filled .docx to PDF.
# poppler-utils (pdftotext) is used as a more reliable text-extraction
# fallback for some suppliers' invoice PDFs (see invoice_ingest.py) --
# pypdf's plain extract_text() sometimes drops/garbles wide table columns.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    fonts-culmus \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Headless Chromium for cashontab_bot.py, which logs into Cash On Tab itself
# to enter goods-receipt documents (no API exists -- see catalog_store.py).
# --with-deps pulls in whatever system libraries Chromium needs; apt's
# package lists were cleaned above so this step re-fetches them itself.
RUN playwright install --with-deps chromium

COPY contract_filler.py web_app.py chat_ui.html esign.py pension_store.py pension_companies.py dashboard.html login.html pension.html ./
COPY gmail_client.py invoice_store.py invoice_ingest.py inventory.html mappings.html branches.py suppliers.py ./
COPY item_matcher.py item_mapping_store.py catalog_store.py cashontab_catalog.json cashontab_bot.py ./
COPY employment_contract_template_ABT.docx employment_contract_template_worker.docx template_piturim.docx template_shimua.docx template_ishur_haaskaa.docx template_betichut.docx template_incident_notice.docx ./

ENV CONTRACT_TEMPLATE_PATH=/app/employment_contract_template_ABT.docx
EXPOSE 8000

# Serves the standalone web-chat page (no WhatsApp/Meta account needed).
CMD ["uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "8000"]
