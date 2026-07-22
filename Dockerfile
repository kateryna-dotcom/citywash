FROM python:3.11-slim

# LibreOffice + Hebrew fonts, needed to convert the filled .docx to PDF
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    fonts-culmus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY contract_filler.py web_app.py chat_ui.html esign.py ./
COPY employment_contract_template_ABT.docx employment_contract_template_worker.docx template_piturim.docx template_shimua.docx template_ishur_haaskaa.docx template_betichut.docx template_incident_notice.docx ./

ENV CONTRACT_TEMPLATE_PATH=/app/employment_contract_template_ABT.docx
EXPOSE 8000

# Serves the standalone web-chat page (no WhatsApp/Meta account needed).
CMD ["uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "8000"]
