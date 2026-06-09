# NPDB Enrollment Reconciliation — Tool

Reconciles a client's credentialing status (SOT) against NPDB enrollment status, flags
issues, validates databank ids, and writes result tabs back to the client's Google Sheet.
Web UI (Streamlit) + reusable core + CLI.

## Files
| File | What |
|---|---|
| `reconcile_core.py` | The logic — `reconcile(sheet_id, sot_tab, npdb_tab, cfg)` returns a summary and writes tabs |
| `app.py` | Streamlit front end |
| `reconcile_cli.py` | Headless/scheduleable CLI |
| `Dockerfile`, `requirements.txt` | Container for Cloud Run |

## How a user uses it
1. **Share** the client Google Sheet (Editor) with the service account:
   `sheet-access@create-494211.iam.gserviceaccount.com`
2. Open the tool → paste the **Sheet URL** → pick the **SOT** and **NPDB** tabs.
3. (Optional) tweak status mappings / match score under *Advanced*.
4. Click **Run** → results are written as tabs (`README`, `Summary`, `Action_Items_All`,
   `Missing_Enrollment`, `Should_Be_Cancelled`, `Duplicates`, `Databank_Updates`, `Reconciliation`).

## Run locally
```bash
pip install -r requirements.txt
# point at the SA key (or set GOOGLE_SA_KEY_JSON to the inline JSON)
set GOOGLE_SA_KEY=C:\path\to\create-494211-....json     # PowerShell: $env:GOOGLE_SA_KEY="..."
streamlit run app.py
```
CLI: `python reconcile_cli.py --sheet <url> --sot SOT --npdb "NPDB Report"`

## Deploy to Cloud Run (production — recommended for PHI)
```bash
# 1. Put the SA key in Secret Manager
gcloud secrets create npdb-sa-key --data-file=create-494211-....json

# 2. Build + push
gcloud builds submit --tag gcr.io/PROJECT/npdb-reconcile

# 3. Deploy, injecting the key as env GOOGLE_SA_KEY_JSON, and require auth
gcloud run deploy npdb-reconcile \
  --image gcr.io/PROJECT/npdb-reconcile \
  --region us-central1 --no-allow-unauthenticated \
  --update-secrets GOOGLE_SA_KEY_JSON=npdb-sa-key:latest
```
Then front it with **IAP** (or `--no-allow-unauthenticated` + grant `run.invoker` to your
CertifyOS Google group) so only authorized users reach it.

## ⚠️ PHI
This processes provider PHI (SSN, DOB). Do **not** deploy on a public host (e.g. Streamlit
Community Cloud). Keep it internal (Cloud Run + IAP), keep the SA key in Secret Manager,
and restrict who can invoke it.

## Reuse for another client
Nothing to change — the user just points the tool at a different sheet/tabs. If a client
uses different status wording, set it in the *Advanced* panel (or `Config`).
