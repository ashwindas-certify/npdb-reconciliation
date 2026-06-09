"""NPDB enrollment reconciliation — importable core (used by the CLI and the Streamlit app).

reconcile(sheet_id, sot_tab, npdb_tab, cfg) -> Result
  reads SOT + NPDB tabs, strong-matches providers to NPDB enrollments, reconciles
  credentialing status vs enrollment status, validates databank ids, writes result
  tabs back to the sheet, and returns a summary for the UI.

Auth: a service account. Provide the key via Config.sa_key_path OR env GOOGLE_SA_KEY
(path) OR GOOGLE_SA_KEY_JSON (inline JSON, e.g. a Cloud Run secret).
"""
from __future__ import annotations
import os, re, json, time
from dataclasses import dataclass, field
from collections import defaultdict, Counter
import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import build
try:
    from rapidfuzz import fuzz
except ImportError:
    fuzz = None

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SA_EMAIL = "sheet-access@create-494211.iam.gserviceaccount.com"   # share client sheets with this

# ----------------------------- config -----------------------------
@dataclass
class Config:
    active_statuses: set     = field(default_factory=lambda: {"cred approved","psv ready","psv complete by certifyos"})
    terminated_statuses: set = field(default_factory=lambda: {"provider terminated","withdrawn/cancelled","cred denied"})
    recred_cycles: set       = field(default_factory=lambda: {"recredentialing"})  # also expect active enrollment
    npdb_active: set         = field(default_factory=lambda: {"enrolled"})
    npdb_cancelled: set      = field(default_factory=lambda: {"canceled","cancelled"})
    accept_score: float      = 45.0
    name_threshold: int      = 85          # fuzz ratio to count a name as corroborating
    sa_key_path: str | None  = None        # falls back to env

@dataclass
class Result:
    total: int
    balanced: bool
    action_count: int
    summary: list                 # list[[label, value]]
    counts: dict
    confidence: dict
    written_tabs: list

# ----------------------------- auth -------------------------------
def get_service(sa_key_path: str | None = None):
    inline = os.environ.get("GOOGLE_SA_KEY_JSON")
    if inline:
        info = json.loads(inline)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        path = sa_key_path or os.environ.get("GOOGLE_SA_KEY") or \
               os.path.join(os.path.expanduser("~"), "Downloads", "create-494211-147f2005e4ac.json")
        creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

def _retry(fn, what=""):
    for a in range(5):
        try: return fn()
        except Exception as e:
            if a == 4: raise
            time.sleep(2*(a+1))

def list_tabs(svc, sheet_id):
    meta = _retry(lambda: svc.spreadsheets().get(spreadsheetId=sheet_id).execute(), "meta")
    return [s["properties"]["title"] for s in meta["sheets"]]

def read_tab(svc, sheet_id, tab):
    vals = _retry(lambda: svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'", valueRenderOption="UNFORMATTED_VALUE"
    ).execute(), f"read {tab}").get("values", [])
    if not vals: return []
    hdr = [str(h).strip() for h in vals[0]]
    return [{hdr[i]: (r[i] if i < len(r) else "") for i in range(len(hdr))} for r in vals[1:]]

# --------------------------- normalizers --------------------------
def npi_n(v):  d = re.sub(r"\D", "", str(v or "")); return d if len(d) == 10 else ""
def ssn4(v):   d = re.sub(r"\D", "", str(v or "")); return d[-4:] if len(d) >= 4 else ""
def name_n(v): return re.sub(r"[^a-z]", "", str(v or "").lower())
def dob_n(v):
    if v in (None, ""): return ""
    ts = pd.to_datetime(str(v), errors="coerce")
    return ts.strftime("%Y-%m-%d") if pd.notna(ts) else ""

# ----------------------------- core -------------------------------
def reconcile(sheet_id: str, sot_tab: str, npdb_tab: str, cfg: Config | None = None,
              write: bool = True, progress=lambda m: None) -> Result:
    cfg = cfg or Config()
    svc = get_service(cfg.sa_key_path)

    def status_class(s):
        s = str(s or "").strip().lower()
        if s in cfg.active_statuses: return "active"
        if s in cfg.terminated_statuses: return "terminated"
        return "in_progress"
    def enroll_class(s):
        s = str(s or "").strip().lower()
        if s in cfg.npdb_active: return "active"
        if s in cfg.npdb_cancelled: return "cancelled"
        return "other"

    progress("Reading SOT…");  sot_raw = read_tab(svc, sheet_id, sot_tab)
    progress("Reading NPDB…"); npdb_raw = read_tab(svc, sheet_id, npdb_tab)
    progress(f"SOT {len(sot_raw):,} rows · NPDB {len(npdb_raw):,} rows — matching…")

    npdb, by_npi, by_ssn4, by_dob_last, by_dob = [], defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for r in npdb_raw:
        rec = {"databank_id": str(r.get("Data Bank Subject ID Number","")).strip(),
               "npi": npi_n(r.get("NPI")), "ssn4": ssn4(r.get("SSN")), "dob": dob_n(r.get("Birthdate")),
               "first": name_n(r.get("First Name")), "last": name_n(r.get("Last Name")),
               "enroll_status": str(r.get("NPDB Enrollment Status","")).strip(),
               "enroll_class": enroll_class(r.get("NPDB Enrollment Status")),
               "entity": str(r.get("Submitted on Behalf of Entity","")).strip(),
               "enroll_start": str(r.get("Enrollment Start Date","")).strip(),
               "cancel_date": str(r.get("Cancellation Date","")).strip(),
               "raw_first": str(r.get("First Name","")).strip(), "raw_last": str(r.get("Last Name","")).strip()}
        i = len(npdb); npdb.append(rec)
        if rec["npi"]:  by_npi[rec["npi"]].append(i)
        if rec["ssn4"]: by_ssn4[rec["ssn4"]].append(i)
        if rec["dob"] and rec["last"]: by_dob_last[(rec["dob"], rec["last"])].append(i)
        if rec["dob"]:  by_dob[rec["dob"]].append(i)

    providers = {}
    for r in sot_raw:
        pid = str(r.get("providerId","")).strip()
        if pid and pid not in providers: providers[pid] = r

    def _ratio(a, b):
        if not a or not b: return 0
        if a == b: return 100
        return fuzz.ratio(a, b) if fuzz else 0

    def match(p):
        pn, s4, dob = npi_n(p.get("npi")), ssn4(p.get("ssn")), dob_n(p.get("dateOfBirth"))
        pf, pl = name_n(p.get("firstName")), name_n(p.get("lastName"))
        cand = set()
        if pn: cand |= set(by_npi.get(pn, []))
        if s4: cand |= set(by_ssn4.get(s4, []))
        if dob and pl: cand |= set(by_dob_last.get((dob, pl), []))
        if not cand and dob: cand |= set(by_dob.get(dob, []))
        bs, best, bi, bb, bln = -1, None, None, [], 0
        for i in cand:
            m = npdb[i]; ln, fn = _ratio(pl, m["last"]), _ratio(pf, m["first"])
            s, b = 0.0, []
            if pn and pn == m["npi"]:  s += 50; b.append("npi")
            if s4 and s4 == m["ssn4"]: s += 30; b.append("ssn4")
            if dob and dob == m["dob"]: s += 20; b.append("dob")
            s += ln*0.15 + fn*0.10
            if ln >= cfg.name_threshold: b.append("last")
            if fn >= cfg.name_threshold: b.append("first")
            anchor = ("npi" in b) or ("ssn4" in b) or ("dob" in b and ln >= cfg.name_threshold)
            if anchor and s > bs: bs, best, bi, bb, bln = s, m, i, b, ln
        if best is None or bs < cfg.accept_score:
            return [], "", 0, "UNMATCHED", ""
        signals = sum(x in bb for x in ("npi","ssn4","dob","last","first"))
        name_ok = ("last" in bb) or ("first" in bb)
        if ("npi" in bb and (name_ok or "dob" in bb)) or signals >= 3: conf = "HIGH"
        elif signals == 2 or "npi" in bb: conf = "MEDIUM"
        else: conf = "LOW"
        conflict = ""
        if "npi" in bb and ((best["last"] and pl and bln < 60) or (best["dob"] and dob and best["dob"] != dob)):
            conflict = "NPI_MATCH_IDENTITY_DIFFERS"
        idxs = by_npi.get(best["npi"], []) if best["npi"] else [bi]
        return idxs, "+".join(bb), round(bs, 1), conf, conflict

    out, dups, db_updates, missing_rows, cancel_rows, action_all = [], [], [], [], [], []
    counts, acct, confc = Counter(), Counter(), Counter()
    n_conflict = 0
    ACTIONABLE = {"MISSING_ENROLLMENT","DUPLICATE_ENROLLMENT","SHOULD_BE_CANCELLED","DATABANK_ID_OUT_OF_SYNC"}
    # NPDB data points each row was compared against (matched record)
    NPDB_HDR  = ["npdb_name","npdb_npi","npdb_dob","npdb_ssn_last4","npdb_databank_id","npdb_enroll_status","npdb_entity"]
    NPDB3_HDR = ["npdb_name","npdb_npi","npdb_dob"]
    def npdb_pts(m):
        if not m: return ["", "", "", "", "", "", ""]
        return [f"{m['raw_last']}, {m['raw_first']}".strip(", "), m["npi"], m["dob"], m["ssn4"],
                m["databank_id"], m["enroll_status"], m["entity"]]
    def npdb3(m):
        return [f"{m['raw_last']}, {m['raw_first']}".strip(", "), m["npi"], m["dob"]] if m else ["","",""]
    def _start_ts(m):
        ts = pd.to_datetime(m["enroll_start"], errors="coerce")
        return ts if pd.notna(ts) else pd.Timestamp.max

    for pid, p in providers.items():
        pname = f"{p.get('firstName','')} {p.get('lastName','')}".strip()
        cls = status_class(p.get("credentialingStatus"))
        idxs, tier, score, conf, conflict = match(p)
        matched = [npdb[i] for i in idxs]
        prim = next((m for m in matched if m["enroll_class"] == "active"), matched[0] if matched else None)
        n_enr = sum(1 for m in matched if m["enroll_class"] == "active")
        n_can = sum(1 for m in matched if m["enroll_class"] == "cancelled")
        n_oth = sum(1 for m in matched if m["enroll_class"] == "other")
        npdb_ids = sorted({m["databank_id"] for m in matched if m["databank_id"]})
        sot_db = str(p.get("databank_subject_id","")).strip()

        # A provider EXPECTS an active enrollment if its status is active, OR it is a
        # Recredentialing provider (already credentialed) — unless terminated/cancelled.
        cyc = str(p.get("credentialingCycle", "")).strip().lower()
        if cls == "terminated":
            expect = "terminated"
        elif cls == "active" or cyc in cfg.recred_cycles:
            expect = "expects_active"
        else:
            expect = "in_progress"

        if expect == "expects_active":
            bucket = "EXP_MISSING" if n_enr == 0 else ("EXP_DUPLICATE" if n_enr > 1 else "EXP_OK")
        elif expect == "terminated":
            bucket = "TERM_SHOULD_CANCEL" if n_enr >= 1 else "TERM_OK"
        else:
            bucket = "IN_PROGRESS"
        acct[bucket] += 1; confc[conf] += 1

        flags = []
        if expect == "expects_active":
            if n_enr == 0:  flags.append("MISSING_ENROLLMENT")
            elif n_enr > 1: flags.append("DUPLICATE_ENROLLMENT")
        elif expect == "terminated" and n_enr >= 1:
            flags.append("SHOULD_BE_CANCELLED")
        if not matched: flags.append("NO_NPDB_MATCH")
        suggested_db = ""
        if matched and (not sot_db or sot_db not in npdb_ids):
            flags.append("DATABANK_ID_OUT_OF_SYNC")
            suggested_db = next((m["databank_id"] for m in matched if m["enroll_class"] == "active"),
                                npdb_ids[0] if npdb_ids else "")
        if conflict:
            flags.append("REVIEW_IDENTITY"); n_conflict += 1
        if not flags:
            flags.append("IN_PROGRESS" if cls == "in_progress" else "OK")
        for f in flags: counts[f] += 1

        npi = npi_n(p.get("npi")); cs = str(p.get("credentialingStatus",""))
        cyc_raw = str(p.get("credentialingCycle",""))
        statuses = "; ".join(sorted({m["enroll_status"] for m in matched}))
        out.append([pid, pname, npi, cs, cyc_raw, cls, expect, tier, score, conf, conflict, len(matched),
                    n_enr, n_can, n_oth, statuses, sot_db, ", ".join(npdb_ids),
                    ("Y" if (sot_db and sot_db in npdb_ids) else ("N" if matched else "")),
                    suggested_db, " | ".join(flags), *npdb_pts(prim)])
        if "DUPLICATE_ENROLLMENT" in flags:
            active_ms = sorted([m for m in matched if m["enroll_class"] == "active"], key=_start_ts)
            for j, m in enumerate(active_ms):   # oldest first -> retain it (max history)
                retain = "KEEP (oldest / max history)" if j == 0 else "cancel"
                dups.append([pid, pname, npi, cs, retain, m["databank_id"], m["enroll_status"],
                             m["entity"], m["enroll_start"], m["cancel_date"], *npdb3(m)])
        if "DATABANK_ID_OUT_OF_SYNC" in flags:
            db_updates.append([pid, pname, npi, cs, ("missing" if not sot_db else "mismatch"),
                               sot_db, suggested_db, statuses,
                               next((m["entity"] for m in matched if m["databank_id"] == suggested_db), ""),
                               *npdb3(prim)])
        if "MISSING_ENROLLMENT" in flags:
            missing_rows.append([pid, pname, npi, cs,
                                 ("NO_NPDB_RECORD" if not matched else "ENROLLMENT_NOT_ACTIVE"),
                                 statuses or "(none)", sot_db, ", ".join(npdb_ids), tier, score, *npdb_pts(prim)])
        if "SHOULD_BE_CANCELLED" in flags:
            for m in matched:
                if m["enroll_class"] == "active":
                    cancel_rows.append([pid, pname, npi, cs, m["databank_id"], m["enroll_status"],
                                        m["entity"], m["enroll_start"], m["cancel_date"], *npdb3(m)])
        acts = [f for f in flags if f in ACTIONABLE]
        if acts:
            action_all.append([pid, pname, npi, cs, cyc_raw, expect, " | ".join(acts), n_enr, n_can,
                               statuses, sot_db, suggested_db, tier, score, conf, conflict, *npdb_pts(prim)])

    # ---- accounting summary ----
    miss_no_rec   = sum(1 for r in missing_rows if r[4] == "NO_NPDB_RECORD")
    miss_inactive = sum(1 for r in missing_rows if r[4] == "ENROLLMENT_NOT_ACTIVE")
    db_missing  = sum(1 for u in db_updates if u[4] == "missing")
    db_mismatch = len(db_updates) - db_missing
    n_active = acct["EXP_OK"] + acct["EXP_MISSING"] + acct["EXP_DUPLICATE"]
    n_term   = acct["TERM_OK"] + acct["TERM_SHOULD_CANCEL"]
    n_prog   = acct["IN_PROGRESS"]
    total    = len(providers); tie = n_active + n_term + n_prog
    summary = [
        ["NPDB Enrollment Reconciliation — Accounting",""],
        ["Total providers", total], ["",""],
        ["Bucket","Providers"],
        ["EXPECTED active enrollment (active status OR Recredentialing)", n_active],
        ["    OK — exactly 1 active enrollment", acct["EXP_OK"]],
        ["    MISSING enrollment", acct["EXP_MISSING"]],
        ["        of which: no NPDB record", miss_no_rec],
        ["        of which: NPDB record Canceled/Suspended", miss_inactive],
        ["    DUPLICATE enrollment (>1 active)", acct["EXP_DUPLICATE"]],
        ["TERMINATED / Cancelled / Denied", n_term],
        ["    OK — no active enrollment", acct["TERM_OK"]],
        ["    SHOULD BE CANCELLED (still Enrolled)", acct["TERM_SHOULD_CANCEL"]],
        ["IN PROGRESS / Other (not evaluated)", n_prog], ["",""],
        ["Sum of buckets (must equal Total)", tie],
        ["Balanced?", "YES" if tie == total else f"NO (diff {total-tie})"], ["",""],
        ["Cross-cutting (overlaps the buckets above)",""],
        ["Databank ID needs update", len(db_updates)],
        ["    missing in SOT (populate)", db_missing],
        ["    mismatch", db_mismatch],
        ["Identity conflicts (NPI matched but name/DOB differ) — review", n_conflict], ["",""],
        ["Match confidence",""],
        ["    HIGH", confc["HIGH"]], ["    MEDIUM", confc["MEDIUM"]],
        ["    LOW", confc["LOW"]], ["    UNMATCHED (no NPDB record)", confc["UNMATCHED"]],
    ]

    headers = {
        "summary": None, "readme": None,
        "reconciliation": ["providerId","provider_name","npi","credentialingStatus","credentialingCycle",
            "status_class","expectation","match_tier","match_score","match_confidence","identity_conflict",
            "npdb_rows_matched","active_enrollments","cancelled_enrollments","other_enrollments","npdb_statuses",
            "sot_databank_id","npdb_databank_ids","databank_in_sync","suggested_databank_id","flags"] + NPDB_HDR,
        "action_items_all": ["providerId","provider_name","npi","credentialingStatus","credentialingCycle",
            "expectation","action_items","active_enrollments","cancelled_enrollments","npdb_statuses",
            "sot_databank_id","suggested_databank_id","match_tier","match_score","match_confidence","identity_conflict"] + NPDB_HDR,
        "missing_enrollment": ["providerId","provider_name","npi","credentialingStatus","missing_type",
            "npdb_statuses","sot_databank_id","npdb_databank_ids","match_tier","match_score"] + NPDB_HDR,
        "should_be_cancelled": ["providerId","provider_name","npi","credentialingStatus","npdb_databank_id",
            "npdb_enroll_status","entity","enroll_start_date","cancel_date"] + NPDB3_HDR,
        "duplicates": ["providerId","provider_name","npi","credentialingStatus","retain","npdb_databank_id",
            "npdb_enroll_status","entity","enroll_start_date","cancel_date"] + NPDB3_HDR,
        "databank_updates": ["providerId","provider_name","npi","credentialingStatus","update_type",
            "current_sot_databank_id","suggested_databank_id","npdb_statuses","entity"] + NPDB3_HDR,
    }
    data = {"summary": summary, "action_items_all": action_all, "missing_enrollment": missing_rows,
            "should_be_cancelled": cancel_rows, "duplicates": dups, "databank_updates": db_updates,
            "reconciliation": out}

    written = []
    if write:
        progress("Writing result tabs…")
        meta = _retry(lambda: svc.spreadsheets().get(spreadsheetId=sheet_id).execute(), "meta")
        existing = {s["properties"]["title"] for s in meta["sheets"]}
        order = ["readme","summary","action_items_all","missing_enrollment","should_be_cancelled",
                 "duplicates","databank_updates","reconciliation"]
        # remove old TitleCase result tabs (renamed to snake_case)
        old_titlecase = {"README","Summary","Action_Items_All","Missing_Enrollment",
                         "Should_Be_Cancelled","Duplicates","Databank_Updates","Reconciliation"}
        del_reqs = [{"deleteSheet": {"sheetId": s["properties"]["sheetId"]}}
                    for s in meta["sheets"] if s["properties"]["title"] in old_titlecase]
        if del_reqs:
            _retry(lambda: svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id,
                body={"requests": del_reqs}).execute(), "remove old tabs")
            existing -= old_titlecase
        data["readme"] = _readme(cfg, sot_tab, npdb_tab)
        for name in order:
            if name not in existing:
                _retry(lambda n=name: svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id,
                    body={"requests":[{"addSheet":{"properties":{"title":n}}}]}).execute(), f"add {name}")
                existing.add(name)
            _retry(lambda n=name: svc.spreadsheets().values().clear(spreadsheetId=sheet_id, range=f"'{n}'").execute(), f"clear {name}")
            hdr = headers.get(name); rows = data[name]
            body = ([hdr] + rows) if hdr else rows
            _retry(lambda n=name, b=body: svc.spreadsheets().values().update(spreadsheetId=sheet_id,
                range=f"'{n}'!A1", valueInputOption="RAW", body={"values": b}).execute(), f"write {name}")
            written.append(name)

    return Result(total=total, balanced=(tie == total), action_count=len(action_all),
                  summary=summary, counts=dict(counts), confidence=dict(confc), written_tabs=written)

def _readme(cfg, sot_tab, npdb_tab):
    return [[x] for x in [
        "NPDB ENROLLMENT RECONCILIATION — README (logic & methodology)","",
        "PURPOSE  Reconcile each provider's credentialing status (SOT) vs NPDB enrollment status;",
        "flag mismatches, validate databank ids, surface action items. Reusable per client.","",
        f"INPUTS  SOT tab '{sot_tab}' (one row/provider) · NPDB tab '{npdb_tab}' (one row/Data Bank Subject ID).","",
        "STATUS MAPPING (SOT -> class)",
        f"  ACTIVE     : {', '.join(sorted(cfg.active_statuses))}",
        f"  TERMINATED : {', '.join(sorted(cfg.terminated_statuses))}",
        "  IN_PROGRESS: everything else (not evaluated for missing/duplicate)",
        f"  RECREDENTIALING cycle ({', '.join(sorted(cfg.recred_cycles))}) ALSO expects an active enrollment",
        "  (already credentialed) unless terminated/cancelled — even when the status is still in-progress.","",
        "NPDB ENROLLMENT STATUS -> class",
        f"  active (enrolled): {', '.join(sorted(cfg.npdb_active))}   cancelled: {', '.join(sorted(cfg.npdb_cancelled))}",
        "  other (e.g. Suspended): not an active enrollment","",
        "MATCHING (scored, multi-field — not NPI alone)",
        "  Candidates share NPI / SSN-last4 / (DOB+last name). Weights: NPI+50, SSN4+30, DOB+20,",
        "  last-name fuzzy up to +15, first +10. Needs an anchor (NPI, SSN4, or DOB+strong last name).",
        "  NPI is primary; when NPI matches we confirm with NAME (DOB/SSN often missing). Accept score >= %d." % int(cfg.accept_score),
        "  Confidence HIGH = NPI+name (or NPI+DOB / 3+ signals); MEDIUM = NPI alone or 2 signals; LOW = weak.",
        "  identity_conflict = NPI matched but name/DOB disagree -> REVIEW.","",
        "FLAGS  MISSING_ENROLLMENT (active, no active enrollment; sub-typed) · DUPLICATE_ENROLLMENT ·",
        "  SHOULD_BE_CANCELLED (terminated still enrolled) · DATABANK_ID_OUT_OF_SYNC (missing/mismatch) ·",
        "  REVIEW_IDENTITY · OK / IN_PROGRESS","",
        "ACCOUNTING (Summary)  mutually-exclusive buckets that sum to total:",
        "  ACTIVE = OK+MISSING+DUPLICATE ; TERMINATED = OK+SHOULD_BE_CANCELLED ; IN_PROGRESS.",
        "  Databank-update & identity-conflict are cross-cutting (overlap the buckets).","",
        "TABS (snake_case)  readme · summary · action_items_all · missing_enrollment ·",
        "  should_be_cancelled · duplicates · databank_updates · reconciliation","",
        "Every result tab also includes the matched NPDB data points it was compared against",
        "(npdb_name, npdb_npi, npdb_dob, npdb_databank_id, npdb_enroll_status, npdb_entity).",
        "duplicates: rows sorted by enrollment start; 'retain' = KEEP the oldest (max history), cancel the rest.","",
        f"Service account (share client sheets with this): {SA_EMAIL}",
    ]]
