FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY reconcile_core.py app.py reconcile_cli.py ./

# Cloud Run sets $PORT (default 8080). The SA key is injected via env GOOGLE_SA_KEY_JSON
# (a Secret Manager secret) — do NOT bake the key into the image.
ENV PORT=8080
EXPOSE 8080
CMD streamlit run app.py --server.port=${PORT} --server.address=0.0.0.0 --server.headless=true
