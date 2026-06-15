"""FastAPI backend for the NPDB Reconciliation tool. Reuses reconcile_core.

Endpoints:
  GET  /api/info             -> service-account email (to show the share-with banner)
  GET  /api/tabs?sheet=...    -> list tab names of a sheet
  POST /api/reconcile         -> run reconciliation, write tabs, return summary
  GET  /api/health
Serves the built React app (backend/static) at /.
"""
import os, sys, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # find reconcile_core
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
from reconcile_core import Config, get_service, list_tabs, reconcile, bq_sot, bq_rows, bq_clients, SA_EMAIL, DEFAULT_BQ_PROJECT

app = FastAPI(title="NPDB Reconciliation")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def _sid(s: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", s or "")
    return m.group(1) if m else (s or "").strip()

def _friendly(e: Exception) -> str:
    m = str(e)
    ml = m.lower()
    if "default credentials" in ml or "could not automatically determine" in ml or "reauth" in ml:
        return "Not logged in to BigQuery. In a terminal run:  gcloud auth application-default login"
    if "above the limit" in ml or "10000000" in m:
        return "The results sheet hit Google Sheets' 10,000,000-cell limit. Use a fresh/empty results sheet, or narrow the query."
    if "403" in m or "permission" in ml or "not have" in ml:
        return "Permission denied. For the sheet: share it (Editor) with the SA. For BigQuery: confirm your account can read the dataset."
    if "404" in m or "not found" in ml:
        return "Sheet or tab not found — check the URL / tab names."
    return m[:300]

@app.get("/api/health")
def health(): return {"ok": True}

@app.get("/api/info")
def info(): return {"sa_email": SA_EMAIL}

@app.get("/api/clients")
def clients():
    try:
        return {"clients": bq_clients(Config())}
    except Exception as e:
        raise HTTPException(status_code=400, detail=_friendly(e))

@app.get("/api/tabs")
def tabs(sheet: str):
    try:
        svc = get_service()
        sid = _sid(sheet)
        return {"sheet_id": sid, "tabs": list_tabs(svc, sid)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=_friendly(e))

class RunReq(BaseModel):
    sheet: str                       # results sheet (in sheet mode it also holds npdb_tab)
    npdb_tab: Optional[str] = None   # NPDB report = a tab of `sheet`…
    npdb_sql: Optional[str] = None   # …OR a BigQuery query returning the NPDB report
    client: Optional[str] = None     # organization name -> SOT pulled from BigQuery
    bq_sql: Optional[str] = None     # optional custom SOT query (overrides the built-in one);
                                     # may reference @client (then a client pick is required)
    sot_tab: Optional[str] = None    # legacy: read SOT from a tab instead of BigQuery
    active: Optional[List[str]] = None
    terminated: Optional[List[str]] = None
    npdb_active: Optional[List[str]] = None
    npdb_cancelled: Optional[List[str]] = None
    accept_score: Optional[float] = None
    client_workbook: Optional[bool] = False   # also create a separate client-facing spreadsheet (summary + recon)

@app.post("/api/reconcile")
def run(req: RunReq):
    cfg = Config()
    if req.active:         cfg.active_statuses = {s.strip().lower() for s in req.active if s.strip()}
    if req.terminated:     cfg.terminated_statuses = {s.strip().lower() for s in req.terminated if s.strip()}
    if req.npdb_active:    cfg.npdb_active = {s.strip().lower() for s in req.npdb_active if s.strip()}
    if req.npdb_cancelled: cfg.npdb_cancelled = {s.strip().lower() for s in req.npdb_cancelled if s.strip()}
    if req.accept_score:   cfg.accept_score = float(req.accept_score)
    clean = lambda s: (s or "").strip().rstrip(";").strip()
    sql, npdb_sql = clean(req.bq_sql), clean(req.npdb_sql)
    if not req.npdb_tab and not npdb_sql:
        raise HTTPException(status_code=400, detail="Pick an NPDB tab or provide an NPDB BigQuery query.")
    if not req.client and not req.sot_tab and not sql:
        raise HTTPException(status_code=400, detail="Pick a client (BigQuery SOT), provide a custom SOT query, or a SOT tab.")
    for label, q in (("SOT", sql), ("NPDB", npdb_sql)):
        if not q: continue
        if not re.match(r"(?is)^(with|select)\b", q):
            raise HTTPException(status_code=400, detail=f"The {label} query must be a single SELECT (or WITH … SELECT) statement.")
        if "@client" in q and not req.client:
            raise HTTPException(status_code=400, detail=f"The {label} query references @client — pick a client too.")
    try:
        if sql:
            params = {"client": req.client} if "@client" in sql else None
            sot_rows = bq_rows(sql, params, project=cfg.bq_project or DEFAULT_BQ_PROJECT)
            if not sot_rows:
                raise ValueError("The custom SOT query returned no rows.")
            if "providerId" not in sot_rows[0]:
                raise ValueError("The custom SOT query must return a providerId column "
                                 "(plus firstName, lastName, npi, dateOfBirth, credentialingStatus, …).")
        else:
            sot_rows = bq_sot(req.client, cfg) if req.client else None
        npdb_rows = None
        if npdb_sql:
            params = {"client": req.client} if "@client" in npdb_sql else None
            npdb_rows = bq_rows(npdb_sql, params, project=cfg.bq_project or DEFAULT_BQ_PROJECT)
            if not npdb_rows:
                raise ValueError("The NPDB query returned no rows.")
        cw_title = f"NPDB Reconciliation — {req.client}" if req.client else "NPDB Enrollment — Client Summary"
        res = reconcile(_sid(req.sheet), req.sot_tab, req.npdb_tab, cfg, write=True,
                        sot_rows=sot_rows, npdb_rows=npdb_rows,
                        client_workbook=bool(req.client_workbook), client_workbook_title=cw_title)
    except Exception as e:
        raise HTTPException(status_code=400, detail=_friendly(e))
    return {"sheet_id": _sid(req.sheet), "total": res.total, "balanced": res.balanced,
            "action_count": res.action_count, "counts": res.counts,
            "confidence": res.confidence, "written_tabs": res.written_tabs, "summary": res.summary,
            "client_workbook_url": res.client_workbook_url}

# ---- serve the built React SPA (if present) ----
_STATIC = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC):
    _assets = os.path.join(_STATIC, "assets")
    if os.path.isdir(_assets):
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")
    @app.get("/")
    def index():
        return FileResponse(os.path.join(_STATIC, "index.html"))
