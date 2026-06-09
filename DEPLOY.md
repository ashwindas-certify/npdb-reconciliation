# NPDB Reconciliation — FastAPI + React web app (deploy)

Production version: React UI served by a FastAPI backend that reuses `reconcile_core.py`.
One container, deploy to **Cloud Run**.

```
npdb_tool/
  reconcile_core.py        # shared logic
  backend/main.py          # FastAPI API + serves the built SPA
  backend/requirements.txt
  frontend/                # React (Vite) UI
  Dockerfile.web           # multi-stage build (node -> python)
```

## Run locally (two terminals)
```bash
# terminal 1 — backend on :8000
cd npdb_tool
python -m pip install -r backend/requirements.txt
set GOOGLE_SA_KEY=C:\path\to\create-494211-....json    # PowerShell: $env:GOOGLE_SA_KEY="..."
python -m uvicorn backend.main:app --reload --port 8000

# terminal 2 — frontend on :5173 (proxies /api -> :8000)
cd npdb_tool/frontend
npm install
npm run dev
# open http://localhost:5173
```
(Single-process option: `npm run build` in frontend/, copy `dist` to `backend/static`, then just run uvicorn and open http://localhost:8000.)

## Deploy to Cloud Run
```bash
# 1. SA key -> Secret Manager (one time)
gcloud secrets create npdb-sa-key --data-file=create-494211-....json

# 2. Build the image (Cloud Build does the node + python stages)
gcloud builds submit --tag gcr.io/PROJECT/npdb-web --gcs-log-dir=gs://… .  # run from npdb_tool/ with Dockerfile.web
#   (or: gcloud builds submit --config with Dockerfile.web; simplest is a cloudbuild.yaml pointing at Dockerfile.web)

# 3. Deploy — inject the key, require auth, allow long requests
gcloud run deploy npdb-web \
  --image gcr.io/PROJECT/npdb-web \
  --region us-central1 \
  --no-allow-unauthenticated \
  --timeout 600 \
  --memory 1Gi \
  --update-secrets GOOGLE_SA_KEY_JSON=npdb-sa-key:latest
```
> To build with a specific Dockerfile name via Cloud Build, add a `cloudbuild.yaml`:
> ```yaml
> steps:
>   - name: gcr.io/cloud-builders/docker
>     args: ["build","-f","Dockerfile.web","-t","gcr.io/$PROJECT_ID/npdb-web","."]
> images: ["gcr.io/$PROJECT_ID/npdb-web"]
> ```
> then `gcloud builds submit --config cloudbuild.yaml .`

## Access control (PHI)
- `--no-allow-unauthenticated` + grant `roles/run.invoker` to your CertifyOS Google group, **or** put **IAP** in front. Only authorized users reach it.
- SA key stays in **Secret Manager** (never in the image / repo).
- Bump `--timeout`/`--memory` if a client sheet is very large.

## Notes
- The reconcile call is synchronous (~30–90 s for a big sheet). Cloud Run handles it within `--timeout`. For very large clients or heavy concurrency, switch to a job-queue pattern (POST returns a job id, poll status) — the core already returns a structured `Result` so that's a small change.
