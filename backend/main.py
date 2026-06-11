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
from reconcile_core import Config, get_service, list_tabs, reconcile, bq_sot, bq_clients, SA_EMAIL

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
    sheet: str                       # the NPDB sheet (holds npdb_tab; receives result tabs)
    npdb_tab: str
    client: Optional[str] = None     # organization name -> SOT pulled from BigQuery
    sot_tab: Optional[str] = None    # legacy: read SOT from a tab instead of BigQuery
    active: Optional[List[str]] = None
    terminated: Optional[List[str]] = None
    npdb_active: Optional[List[str]] = None
    npdb_cancelled: Optional[List[str]] = None
    accept_score: Optional[float] = None

@app.post("/api/reconcile")
def run(req: RunReq):
    cfg = Config()
    if req.active:         cfg.active_statuses = {s.strip().lower() for s in req.active if s.strip()}
    if req.terminated:     cfg.terminated_statuses = {s.strip().lower() for s in req.terminated if s.strip()}
    if req.npdb_active:    cfg.npdb_active = {s.strip().lower() for s in req.npdb_active if s.strip()}
    if req.npdb_cancelled: cfg.npdb_cancelled = {s.strip().lower() for s in req.npdb_cancelled if s.strip()}
    if req.accept_score:   cfg.accept_score = float(req.accept_score)
    if not req.client and not req.sot_tab:
        raise HTTPException(status_code=400, detail="Pick a client (BigQuery SOT) or a SOT tab.")
    try:
        sot_rows = bq_sot(req.client, cfg) if req.client else None
        res = reconcile(_sid(req.sheet), req.sot_tab, req.npdb_tab, cfg, write=True, sot_rows=sot_rows)
    except Exception as e:
        raise HTTPException(status_code=400, detail=_friendly(e))
    return {"sheet_id": _sid(req.sheet), "total": res.total, "balanced": res.balanced,
            "action_count": res.action_count, "counts": res.counts,
            "confidence": res.confidence, "written_tabs": res.written_tabs, "summary": res.summary}

# ---- serve the built React SPA (if present) ----
_STATIC = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC):
    _assets = os.path.join(_STATIC, "assets")
    if os.path.isdir(_assets):
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")
    @app.get("/")
    def index():
        return FileResponse(os.path.join(_STATIC, "index.html"))
