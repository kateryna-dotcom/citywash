FROM python:3.11-slim

# LibreOffice + Hebrew fonts, needed to convert the filled .docx to PDF.
# poppler-utils (pdftotext) is used as a more reliable text-extraction
# fallback for some suppliers' invoice PDFs (see invoice_ingest.py) --
# pypdf's plain extract_text() sometimes drops/garbles wide table columns.
#
# The rest of this list is headless Chromium's own runtime libraries, for
# cashontab_bot.py (logs into Cash On Tab itself -- no API exists, see
# catalog_store.py). Installed explicitly rather than via `playwright
# install --with-deps`, which fails on this base image (tries to install
# ttf-unifont/ttf-ubuntu-font-family, renamed/removed in its apt repos).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    fonts-culmus \
    poppler-utils \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Just the browser binary -- OS deps were installed explicitly above.
RUN playwright install chromium

COPY contract_filler.py web_app.py chat_ui.html esign.py pension_store.py pension_companies.py dashboard.html login.html pension.html ./
COPY gmail_client.py invoice_store.py invoice_ingest.py inventory.html mappings.html branches.py suppliers.py ./
COPY item_matcher.py item_mapping_store.py catalog_store.py cashontab_catalog.json cashontab_bot.py ./
COPY employment_contract_template_ABT.docx employment_contract_template_worker.docx template_piturim.docx template_shimua.docx template_ishur_haaskaa.docx template_betichut.docx template_incident_notice.docx ./

ENV CONTRACT_TEMPLATE_PATH=/app/employment_contract_template_ABT.docx
EXPOSE 8000

# Serves the standalone web-chat page (no WhatsApp/Meta account needed).
CMD ["uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "8000"]
