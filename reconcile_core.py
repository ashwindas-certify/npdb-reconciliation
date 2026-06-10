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
    mid_threshold: int       = 85          # fuzz ratio to count middle name as corroborating
    link_confidence: str     = "HIGH"      # min reverse-match confidence to suggest LINKing an extra enrollment to a provider
    # SOT identity columns — None = auto-detect from the SOT header (override if detection is wrong)
    sot_license_col: str | None       = None   # license number column in SOT
    sot_license_state_col: str | None = None   # license issuing-state column in SOT
    sot_middle_col: str | None        = None   # middle-name column in SOT (usually absent)
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
    extra_enrollments: int = 0    # NPDB enrollment records (persons) with no provider in the SOT

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
def lic_n(v):  return re.sub(r"[^a-z0-9]", "", str(v or "").lower())      # normalize a license number
def st_n(v):
    s = re.sub(r"[^A-Za-z]", "", str(v or "")).upper()
    return s if len(s) == 2 else ""                                       # only trust a clean 2-letter state
def dob_n(v):
    if v in (None, ""): return ""
    ts = pd.to_datetime(str(v), errors="coerce")
    return ts.strftime("%Y-%m-%d") if pd.notna(ts) else ""

def parse_npdb_license(v):
    """NPDB 'License' is a compound string, e.g. 'Physician (MD) - 7324191-6004 - UT',
    'Professional Counselor - LCPC - 4911 - ID', or 'Social Worker - No License'.
    Returns (normalized_license_number, state). State = trailing 2-letter token;
    license number = the segment just before it (the most reliable id slot)."""
    parts = [p.strip() for p in str(v or "").split(" - ") if p.strip()]
    if not parts: return "", ""
    state = ""
    if len(parts) >= 2 and re.fullmatch(r"[A-Za-z]{2}", parts[-1]):
        state = parts[-1].upper(); parts = parts[:-1]
    licnum = ""
    if len(parts) >= 2 and parts[-1].lower() not in ("no license", "none", "n/a"):
        licnum = lic_n(parts[-1])
    return licnum, state

_CONF_ORDER = {"NONE": 0, "UNMATCHED": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
def _conf_rank(c): return _CONF_ORDER.get(str(c or "").strip().upper(), 0)

def _detect_col(keys, exacts, contains_all=(), contains_any=(), avoid=()):
    """Find a header in `keys` by exact (case-insensitive) match, else by substring rules."""
    low = {str(k).strip().lower(): k for k in keys}
    for e in exacts:
        if e in low: return low[e]
    for lk, orig in low.items():
        if any(a in lk for a in avoid): continue
        if all(c in lk for c in contains_all) and (not contains_any or any(c in lk for c in contains_any)):
            return orig
    return None

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

    npdb = []
    by_npi, by_ssn4, by_dob_last, by_dob, by_licnum = (defaultdict(list), defaultdict(list),
                                                       defaultdict(list), defaultdict(list), defaultdict(list))
    for r in npdb_raw:
        licnum, lstate = parse_npdb_license(r.get("License"))
        rec = {"databank_id": str(r.get("Data Bank Subject ID Number","")).strip(),
               "npi": npi_n(r.get("NPI")), "ssn4": ssn4(r.get("SSN")), "dob": dob_n(r.get("Birthdate")),
               "first": name_n(r.get("First Name")), "last": name_n(r.get("Last Name")),
               "middle": name_n(r.get("Middle Name")),
               "licnum": licnum, "state": lstate, "raw_license": str(r.get("License","")).strip(),
               "enroll_status": str(r.get("NPDB Enrollment Status","")).strip(),
               "enroll_class": enroll_class(r.get("NPDB Enrollment Status")),
               "entity": str(r.get("Submitted on Behalf of Entity","")).strip(),
               "enroll_start": str(r.get("Enrollment Start Date","")).strip(),
               "cancel_date": str(r.get("Cancellation Date","")).strip(),
               "raw_first": str(r.get("First Name","")).strip(), "raw_last": str(r.get("Last Name","")).strip(),
               "raw_middle": str(r.get("Middle Name","")).strip()}
        i = len(npdb); npdb.append(rec)
        if rec["npi"]:  by_npi[rec["npi"]].append(i)
        if rec["ssn4"]: by_ssn4[rec["ssn4"]].append(i)
        if rec["dob"] and rec["last"]: by_dob_last[(rec["dob"], rec["last"])].append(i)
        if rec["dob"]:  by_dob[rec["dob"]].append(i)
        if rec["licnum"]: by_licnum[rec["licnum"]].append(i)

    # detect SOT identity columns (license #, license state, middle name) unless overridden
    sot_keys = list(sot_raw[0].keys()) if sot_raw else []
    lic_col = cfg.sot_license_col or _detect_col(
        sot_keys, exacts=("license","licensenumber","license_number","licenseno","license#"),
        contains_all=("licen",), contains_any=("number","no","num","#",""), avoid=("state","type","status","expir","date"))
    lic_state_col = cfg.sot_license_state_col or _detect_col(
        sot_keys, exacts=("licensestate","license_state","stateoflicense","stateoflicensure","issuingstate","licensingstate"),
        contains_all=("licen","state"))
    if lic_state_col is None:
        lic_state_col = cfg.sot_license_state_col or _detect_col(
            sot_keys, exacts=("state",), contains_all=("state",), avoid=("home","mailing","work","practice","entity","city","zip"))
    mid_col = cfg.sot_middle_col or _detect_col(
        sot_keys, exacts=("middlename","middle_name","middle"), contains_all=("middle",))
    progress(f"SOT identity cols — license:{lic_col or '—'} state:{lic_state_col or '—'} middle:{mid_col or '—'}")

    providers, prov_lic = {}, defaultdict(set)
    for r in sot_raw:
        pid = str(r.get("providerId","")).strip()
        if not pid: continue
        if pid not in providers: providers[pid] = r
        ln_ = lic_n(r.get(lic_col)) if lic_col else ""
        if ln_: prov_lic[pid].add((ln_, st_n(r.get(lic_state_col)) if lic_state_col else ""))

    def _ratio(a, b):
        if not a or not b: return 0
        if a == b: return 100
        return fuzz.ratio(a, b) if fuzz else 0

    def _prov_ident(p):
        """Normalized identity for a SOT provider row (incl. its license set)."""
        pid = str(p.get("providerId","")).strip()
        return {"npi": npi_n(p.get("npi")), "ssn4": ssn4(p.get("ssn")), "dob": dob_n(p.get("dateOfBirth")),
                "first": name_n(p.get("firstName")), "last": name_n(p.get("lastName")),
                "middle": name_n(p.get(mid_col)) if mid_col else "", "lic": prov_lic.get(pid, set())}

    def _score(pi, m):
        """Score identity `pi` against one NPDB record `m`. Returns (score, basis_list, last_ratio).
        Identity points: NPI 50, SSN-last4 30, DOB 20, license# 25; fuzzy last+first up to 25;
        middle name +5, license state +5 (corroborators)."""
        ln, fn = _ratio(pi["last"], m["last"]), _ratio(pi["first"], m["first"])
        licnums = {x[0] for x in pi["lic"] if x[0]}; states = {x[1] for x in pi["lic"] if x[1]}
        s, b = 0.0, []
        if pi["npi"]  and pi["npi"]  == m["npi"]:  s += 50; b.append("npi")
        if pi["ssn4"] and pi["ssn4"] == m["ssn4"]: s += 30; b.append("ssn4")
        if pi["dob"]  and pi["dob"]  == m["dob"]:  s += 20; b.append("dob")
        if m["licnum"] and m["licnum"] in licnums: s += 25; b.append("license")
        s += ln*0.15 + fn*0.10
        if pi["middle"] and m["middle"] and _ratio(pi["middle"], m["middle"]) >= cfg.mid_threshold:
            s += 5; b.append("middle")
        if m["state"] and m["state"] in states: s += 5; b.append("state")
        if ln >= cfg.name_threshold: b.append("last")
        if fn >= cfg.name_threshold: b.append("first")
        return s, b, ln

    def _confidence(b):
        strong = sum(x in b for x in ("npi","ssn4","dob","license"))   # identity-grade signals
        name_ok = ("last" in b) or ("first" in b)
        support = name_ok or ("middle" in b) or ("state" in b)
        if (("npi" in b) and (support or "dob" in b or "license" in b)) \
           or (("license" in b) and ("state" in b) and name_ok) or strong >= 3:
            return "HIGH"
        if strong >= 2 or ("npi" in b) or (("license" in b) and ("state" in b)):
            return "MEDIUM"
        return "LOW"

    def _anchored(b, ln):
        """A match needs a real identity anchor, not name similarity alone."""
        return ("npi" in b) or ("ssn4" in b) or ("dob" in b and ln >= cfg.name_threshold) \
               or ("license" in b and (("state" in b) or ("last" in b) or ("first" in b)))

    def match(p):
        pi = _prov_ident(p)
        cand = set()
        if pi["npi"]: cand |= set(by_npi.get(pi["npi"], []))
        if pi["ssn4"]: cand |= set(by_ssn4.get(pi["ssn4"], []))
        if pi["dob"] and pi["last"]: cand |= set(by_dob_last.get((pi["dob"], pi["last"]), []))
        for lnum, _st in pi["lic"]:
            if lnum: cand |= set(by_licnum.get(lnum, []))
        if not cand and pi["dob"]: cand |= set(by_dob.get(pi["dob"], []))
        bs, best, bi, bb, bln = -1, None, None, [], 0
        for i in cand:
            s, b, ln = _score(pi, npdb[i])
            if _anchored(b, ln) and s > bs: bs, best, bi, bb, bln = s, npdb[i], i, b, ln
        if best is None or bs < cfg.accept_score:
            return [], "", 0, "UNMATCHED", ""
        conf = _confidence(bb)
        conflict = ""
        if "npi" in bb and ((best["last"] and pi["last"] and bln < 60) or (best["dob"] and pi["dob"] and best["dob"] != pi["dob"])):
            conflict = "NPI_MATCH_IDENTITY_DIFFERS"
        idxs = by_npi.get(best["npi"], []) if best["npi"] else [bi]
        return idxs, "+".join(bb), round(bs, 1), conf, conflict

    out, dups, db_updates, missing_rows, cancel_rows, action_all = [], [], [], [], [], []
    counts, acct, confc = Counter(), Counter(), Counter()
    n_conflict = 0
    matched_any = set()        # NPDB row indices claimed by some SOT provider (for the reverse pass)
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
        matched_any.update(idxs)
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

    # ===================== REVERSE PASS: NPDB enrollments not in our SOT =====================
    # Every NPDB row no provider claimed is an "extra" enrollment. Group those by person,
    # try to identify the correct SOT provider with high confidence (reverse match); if none,
    # the person is genuinely not in the SOT -> emit a ready-to-append SOT row.
    progress("Reverse pass — NPDB enrollments not in SOT…")
    # SOT indexes for reverse matching
    sot_idents, s_by_npi, s_by_ssn4, s_by_dob_last, s_by_licnum = {}, defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for pid, p in providers.items():
        pi = _prov_ident(p); sot_idents[pid] = pi
        if pi["npi"]:  s_by_npi[pi["npi"]].append(pid)
        if pi["ssn4"]: s_by_ssn4[pi["ssn4"]].append(pid)
        if pi["dob"] and pi["last"]: s_by_dob_last[(pi["dob"], pi["last"])].append(pid)
        for lnum, _st in pi["lic"]:
            if lnum: s_by_licnum[lnum].append(pid)

    def reverse_match(m):
        """Best SOT provider for an unmatched NPDB record. Returns (pid, basis, score, confidence)."""
        cand = set(s_by_npi.get(m["npi"], [])) | set(s_by_ssn4.get(m["ssn4"], []))
        if m["dob"] and m["last"]: cand |= set(s_by_dob_last.get((m["dob"], m["last"]), []))
        if m["licnum"]: cand |= set(s_by_licnum.get(m["licnum"], []))
        bs, bpid, bb, bln = -1, "", [], 0
        for pid in cand:
            s, b, ln = _score(sot_idents[pid], m)
            if _anchored(b, ln) and s > bs: bs, bpid, bb, bln = s, pid, b, ln
        if not bpid or bs < cfg.accept_score: return "", "", 0, "NONE"
        return bpid, "+".join(bb), round(bs, 1), _confidence(bb)

    # group unmatched NPDB rows into persons (by NPI, else databank id, else DOB+last name)
    extra_groups = defaultdict(list)
    for i, m in enumerate(npdb):
        if i in matched_any: continue
        key = ("npi", m["npi"]) if m["npi"] else \
              (("db", m["databank_id"]) if m["databank_id"] else ("dl", m["dob"], m["last"], m["first"]))
        extra_groups[key].append(m)

    extra_rows, n_extra_link, n_extra_new = [], 0, 0
    for _key, ms in extra_groups.items():
        rep = next((x for x in ms if x["enroll_class"] == "active"), ms[0])   # prefer an active record
        pid, rbasis, rscore, rconf = reverse_match(rep)
        link = bool(pid) and _conf_rank(rconf) >= _conf_rank(cfg.link_confidence)
        disp = "LINK_TO_PROVIDER" if link else "ADD_TO_SOT"
        if link: n_extra_link += 1
        else:    n_extra_new += 1
        db_ids   = sorted({x["databank_id"] for x in ms if x["databank_id"]})
        licenses = sorted({x["raw_license"] for x in ms if x["raw_license"]})
        states   = sorted({x["state"] for x in ms if x["state"]})
        statuses = "; ".join(sorted({x["enroll_status"] for x in ms if x["enroll_status"]}))
        starts   = "; ".join(sorted({x["enroll_start"] for x in ms if x["enroll_start"]}))
        name     = f"{rep['raw_last']}, {rep['raw_first']}".strip(", ")
        extra_rows.append([
            disp, (pid if link else ""), (rconf if pid else "NONE"), (rbasis if pid else ""), rscore,
            ", ".join(db_ids), name, rep["npi"], rep["dob"], rep["ssn4"],
            " | ".join(licenses), ", ".join(states), rep["entity"], statuses, starts, len(ms),
            # ready-to-append SOT row (providerId blank — to create/link):
            "", rep["raw_first"], rep["raw_last"], rep["raw_middle"], rep["npi"], rep["dob"],
            rep["ssn4"], " | ".join(licenses), ", ".join(states)])
    # LINK rows first (actionable now), then ADD rows; within each, most enrollments first
    extra_rows.sort(key=lambda r: (r[0] != "LINK_TO_PROVIDER", -r[15]))

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
        ["Extra NPDB enrollments NOT in SOT (reverse pass)",""],
        ["    distinct persons in NPDB with no SOT provider", len(extra_groups)],
        ["        of which: high-confidence link to an existing provider", n_extra_link],
        ["        of which: not in SOT — suggest adding", n_extra_new],
        ["    (enrollment records behind them)", sum(len(v) for v in extra_groups.values())], ["",""],
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
        "extra_enrollments": ["disposition","suggested_providerId","match_confidence","match_basis","match_score",
            "npdb_databank_ids","npdb_name","npdb_npi","npdb_dob","npdb_ssn_last4","npdb_licenses","npdb_states",
            "npdb_entity","npdb_enroll_statuses","enroll_start_dates","npdb_record_count",
            "append_providerId","append_firstName","append_lastName","append_middleName","append_npi",
            "append_dateOfBirth","append_ssn_last4","append_license","append_license_state"],
    }
    data = {"summary": summary, "action_items_all": action_all, "missing_enrollment": missing_rows,
            "should_be_cancelled": cancel_rows, "duplicates": dups, "databank_updates": db_updates,
            "extra_enrollments": extra_rows, "reconciliation": out}

    written = []
    if write:
        progress("Writing result tabs…")
        meta = _retry(lambda: svc.spreadsheets().get(spreadsheetId=sheet_id).execute(), "meta")
        existing = {s["properties"]["title"] for s in meta["sheets"]}
        order = ["readme","summary","action_items_all","missing_enrollment","should_be_cancelled",
                 "duplicates","databank_updates","extra_enrollments","reconciliation"]
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
                  summary=summary, counts=dict(counts), confidence=dict(confc), written_tabs=written,
                  extra_enrollments=len(extra_groups))

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
        "IDENTITY MATCHING (scored, multi-field — not NPI alone)",
        "  Signals & weights: NPI +50, SSN-last4 +30, DOB +20, license# +25 (license# parsed from the NPDB",
        "  'License' field, e.g. 'Physician (MD) - 7324191-6004 - UT'); last-name fuzzy up to +15, first +10;",
        "  middle name +5, license state +5. Candidates share NPI / SSN4 / (DOB+last name) / license#.",
        "  Needs an anchor: NPI, SSN4, DOB+strong last name, OR license#+(state or name).",
        "  Accept best candidate with score >= %d." % int(cfg.accept_score),
        "  Confidence HIGH = NPI+corroboration (name/DOB/license), or license#+state+name, or 3+ identity signals;",
        "  MEDIUM = NPI alone / license#+state / any 2 identity signals; LOW = weak. SOT license #/state columns",
        "  are auto-detected from the SOT header (override via Config.sot_license_col / sot_license_state_col).",
        "  identity_conflict = NPI matched but name/DOB disagree -> REVIEW.","",
        "FLAGS  MISSING_ENROLLMENT (active, no active enrollment; sub-typed) · DUPLICATE_ENROLLMENT ·",
        "  SHOULD_BE_CANCELLED (terminated still enrolled) · DATABANK_ID_OUT_OF_SYNC (missing/mismatch) ·",
        "  REVIEW_IDENTITY · OK / IN_PROGRESS","",
        "EXTRA ENROLLMENTS NOT IN SOT (reverse pass)  Every NPDB enrollment no provider claimed is grouped by",
        "  person and reverse-matched back to the SOT with the SAME identity algo. disposition LINK_TO_PROVIDER",
        f"  (suggested_providerId; reverse match >= {cfg.link_confidence} confidence) vs ADD_TO_SOT (genuinely not in SOT —",
        "  the append_* columns are a ready-to-append SOT row, providerId blank).","",
        "ACCOUNTING (Summary)  mutually-exclusive buckets that sum to total:",
        "  ACTIVE = OK+MISSING+DUPLICATE ; TERMINATED = OK+SHOULD_BE_CANCELLED ; IN_PROGRESS.",
        "  Databank-update, identity-conflict & extra-enrollments are cross-cutting (NPDB-side, not in the buckets).","",
        "TABS (snake_case)  readme · summary · action_items_all · missing_enrollment ·",
        "  should_be_cancelled · duplicates · databank_updates · extra_enrollments · reconciliation","",
        "Every result tab also includes the matched NPDB data points it was compared against",
        "(npdb_name, npdb_npi, npdb_dob, npdb_databank_id, npdb_enroll_status, npdb_entity).",
        "duplicates: rows sorted by enrollment start; 'retain' = KEEP the oldest (max history), cancel the rest.","",
        f"Service account (share client sheets with this): {SA_EMAIL}",
    ]]
