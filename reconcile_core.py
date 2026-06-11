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

# ----------------- BigQuery SOT source (CertifyOS) -----------------
# Default project + queries. SOT is filtered by organization name via @client (parameterized).
# Auth is YOUR local ADC (gcloud auth application-default login) — no service account.
DEFAULT_BQ_PROJECT = "certifyos-production-platform"

SOT_SQL = """
WITH state_licenses AS (
  SELECT
    e.edit_provider_id,
    STRING_AGG(CONCAT(sl.state, ': ', sl.license_number), ', '
      ORDER BY sl.state, sl.license_number) AS state_licenses
  FROM `certifyos-production-platform.appdb_data.edit_providers` e
  JOIN `certifyos-production-platform.appdb_data.edit_providers_state_licenses` sl
    ON sl.edit_provider_id = e.edit_provider_id
  WHERE sl.state IS NOT NULL AND sl.license_number IS NOT NULL
    AND sl.state IN UNNEST(SPLIT(COALESCE(e.assignedStates, ''), ', '))
  GROUP BY e.edit_provider_id
),
npdb AS (
  SELECT * EXCEPT(rn) FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY edit_provider_id ORDER BY updated_at DESC) AS rn
    FROM `certifyos-production-platform.appdb_data.edit_providers_npdb_data`
  ) WHERE rn = 1
),
caqh AS (
  SELECT * EXCEPT(rn) FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY edit_provider_id ORDER BY updated_at DESC) AS rn
    FROM `certifyos-production-platform.appdb_data.edit_providers_caqh_data`
  ) WHERE rn = 1
)
SELECT
  e.providerId, e.firstName, e.lastName, e.middleName, e.dateOfBirth, e.npi,
  e.credentialingStatus,
  IFNULL(e.credentialingCycle, 'Recredentialing') AS credentialingCycle,
  npdb.databank_subject_id,
  sl.state_licenses,
  JSON_VALUE(caqh.practitioner_information, '$.gender') AS gender,
  JSON_VALUE(caqh.practitioner_information, '$.ssn') AS ssn,
  CONCAT(
    COALESCE(JSON_VALUE(caqh.practitioner_information, '$.homeAddress.address1'), ''), ', ',
    COALESCE(JSON_VALUE(caqh.practitioner_information, '$.homeAddress.city'), ''), ', ',
    COALESCE(JSON_VALUE(caqh.practitioner_information, '$.homeAddress.state'), ''), ' ',
    COALESCE(JSON_VALUE(caqh.practitioner_information, '$.homeAddress.zipCode'), '')
  ) AS home_full_address,
  e.businessPurpose_isForCredentialing
FROM `certifyos-production-platform.appdb_data.edit_providers` e
LEFT JOIN npdb ON npdb.edit_provider_id = e.edit_provider_id
LEFT JOIN caqh ON caqh.edit_provider_id = e.edit_provider_id
LEFT JOIN state_licenses sl ON sl.edit_provider_id = e.edit_provider_id
LEFT JOIN `certifyos-production-platform.appdb_data.organizations` AS o
  ON e.organizationId = o.document_id
WHERE o.name = @client
"""

# Dropdown = organizations that actually have providers, alphabetised.
CLIENTS_SQL = """
SELECT DISTINCT o.name
FROM `certifyos-production-platform.appdb_data.edit_providers` e
JOIN `certifyos-production-platform.appdb_data.organizations` o
  ON e.organizationId = o.document_id
WHERE o.name IS NOT NULL
ORDER BY o.name
"""

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
    sot_gender_col: str | None        = None   # gender/sex column in SOT (corroborating identity signal)
    sot_for_cred_col: str | None      = None   # businessPurpose_isForCredentialing col (only some SOTs);
                                               # true -> direct provider (expect enrollment), false -> delegated (no enrollment)
    npdb_gender_field: str | None     = None   # gender/sex field in the NPDB report (auto-detected; usually 'Sex')
    max_rows_per_tab: int    = 100000      # split a result tab into <name>_2, _3… past this many rows (0 = never)
    sa_key_path: str | None  = None        # falls back to env
    # --- BigQuery SOT source (auth = your local ADC; no service account) ---
    bq_project: str | None     = None      # GCP project to bill/run the query in
    bq_sot_sql: str | None     = None      # SOT query, must filter on @client, e.g.
                                           #   SELECT * FROM `proj.ds.sot` WHERE organization = @client
    bq_clients_sql: str | None = None      # dropdown list, e.g.
                                           #   SELECT DISTINCT name FROM `proj.ds.organization_table` ORDER BY 1

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

# --------------------------- BigQuery (SOT source) ---------------------------
# Auth is YOUR local Application Default Credentials — NO service account needed.
# One-time:  gcloud auth application-default login
def bq_rows(sql: str, params: dict | None = None, project: str | None = None):
    """Run a parameterized query and return rows as list[dict] — same shape as read_tab(),
    so the rest of reconcile() is source-agnostic. `params` -> @name STRING params,
    e.g. bq_rows(SOT_SQL, {'client': 'Headway'}). Values come back native (dates/bools);
    the normalizers str()-coerce them, so no extra casting is needed."""
    from google.cloud import bigquery
    client = bigquery.Client(project=project) if project else bigquery.Client()
    job_cfg = None
    if params:
        job_cfg = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter(k, "STRING", v) for k, v in params.items()])
    # NULL -> "" so BigQuery rows behave like (empty) sheet cells for the normalizers
    return [{k: ("" if v is None else v) for k, v in r.items()}
            for r in client.query(sql, job_config=job_cfg).result()]

def bq_clients(cfg: "Config"):
    """Distinct client/organization names for the dropdown (first column of the clients query)."""
    sql = cfg.bq_clients_sql or CLIENTS_SQL
    return [str(list(r.values())[0]) for r in bq_rows(sql, project=cfg.bq_project or DEFAULT_BQ_PROJECT)
            if list(r.values())[0] not in (None, "")]

def bq_sot(client: str, cfg: "Config"):
    """SOT rows for one client (organization name) from BigQuery — SOT_SQL with @client."""
    sql = cfg.bq_sot_sql or SOT_SQL
    return bq_rows(sql, {"client": client}, project=cfg.bq_project or DEFAULT_BQ_PROJECT)

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
def gender_n(v):
    c = str(v or "").strip().upper()[:1]
    return c if c in ("M", "F") else ""                                   # MALE/FEMALE/M/F -> M/F, else blank
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

def parse_sot_licenses(raw, fallback_state=""):
    """Parse a SOT license cell into a set of (licnum_normalized, state) pairs.
    Handles MULTIPLE comma/semicolon/pipe/newline-separated licenses, each optionally
    prefixed with a 2-letter state + colon — e.g. 'FL: APRN11006437, FL: RN9531639'
    -> {('aprn11006437','FL'), ('rn9531639','FL')}. Tokens with no state prefix use
    `fallback_state` (from a separate license-state column, if any). For an alphanumeric
    number we also add a digits-only variant so it matches whichever form NPDB carries."""
    out = set()
    fb = st_n(fallback_state)
    for tok in re.split(r"[,\n;|]+", str(raw or "")):
        tok = tok.strip()
        if not tok: continue
        st = fb
        m = re.match(r"^([A-Za-z]{2})\s*:\s*(.+)$", tok)   # only ':' marks a state prefix (avoids 'MD-…' ambiguity)
        if m:
            st = m.group(1).upper(); tok = m.group(2).strip()
        if tok.lower() in ("no license", "none", "n/a", "na", "null"): continue
        num = lic_n(tok)
        if not num: continue
        out.add((num, st))
        digits = re.sub(r"\D", "", num)
        if digits and digits != num and len(digits) >= 4:
            out.add((digits, st))
    return out

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
def reconcile(sheet_id: str, sot_tab: str | None, npdb_tab: str, cfg: Config | None = None,
              write: bool = True, progress=lambda m: None, sot_rows: list | None = None) -> Result:
    """`sheet_id` holds the NPDB report tab (`npdb_tab`) and receives the result tabs.
    SOT comes from either a tab in that sheet (`sot_tab`) OR pre-fetched rows passed as
    `sot_rows` (e.g. from BigQuery via bq_sot(client, cfg)) — exactly one of the two."""
    cfg = cfg or Config()
    if sot_rows is None and not sot_tab:
        raise ValueError("provide sot_tab (read from sheet) or sot_rows (e.g. from BigQuery)")
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

    if sot_rows is not None:
        progress(f"Using {len(sot_rows):,} SOT rows from BigQuery…"); sot_raw = sot_rows
    else:
        progress("Reading SOT…"); sot_raw = read_tab(svc, sheet_id, sot_tab)
    progress("Reading NPDB…"); npdb_raw = read_tab(svc, sheet_id, npdb_tab)
    progress(f"SOT {len(sot_raw):,} rows · NPDB {len(npdb_raw):,} rows — matching…")

    npdb = []
    npdb_keys = list(npdb_raw[0].keys()) if npdb_raw else []
    npdb_gender_key = cfg.npdb_gender_field or _detect_col(
        npdb_keys, exacts=("sex","gender","subject sex","subject gender"), contains_any=("gender","sex"))
    by_npi, by_ssn4, by_dob_last, by_dob, by_licnum = (defaultdict(list), defaultdict(list),
                                                       defaultdict(list), defaultdict(list), defaultdict(list))
    for r in npdb_raw:
        licnum, lstate = parse_npdb_license(r.get("License"))
        rec = {"databank_id": str(r.get("Data Bank Subject ID Number","")).strip(),
               "npi": npi_n(r.get("NPI")), "ssn4": ssn4(r.get("SSN")), "dob": dob_n(r.get("Birthdate")),
               "first": name_n(r.get("First Name")), "last": name_n(r.get("Last Name")),
               "middle": name_n(r.get("Middle Name")),
               "gender": gender_n(r.get(npdb_gender_key)) if npdb_gender_key else "",
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
    # license column may be combined & state-prefixed (e.g. 'FL: APRN11006437, FL: RN9531639').
    # exacts cover the combined forms (licenses/stateLicenses) and bypass the 'state' avoid below.
    lic_col = cfg.sot_license_col or _detect_col(
        sot_keys, exacts=("license","licenses","statelicense","statelicenses","licensenumber","licensenumbers",
                          "license_number","state_licenses","licenseno","license#"),
        contains_all=("licen",), contains_any=("number","no","num","#",""), avoid=("state","type","status","expir","date"))
    # separate license-state column (only some SOTs have one; many embed state per-license instead)
    lic_state_col = cfg.sot_license_state_col or _detect_col(
        sot_keys, exacts=("licensestate","license_state","stateoflicense","stateoflicensure","issuingstate","licensingstate"),
        contains_all=("licen","state"))
    if lic_state_col is None:
        lic_state_col = cfg.sot_license_state_col or _detect_col(
            sot_keys, exacts=("state",), contains_all=("state",),
            avoid=("home","mailing","work","practice","entity","city","zip","licen","address","birth"))
    if lic_state_col == lic_col:   # the combined license column is NOT a standalone state column
        lic_state_col = None
    mid_col = cfg.sot_middle_col or _detect_col(
        sot_keys, exacts=("middlename","middle_name","middle"), contains_all=("middle",))
    # businessPurpose_isForCredentialing — present in only some SOTs. true=direct (expect enrollment),
    # false=delegated (no active enrollment wanted).
    forcred_col = cfg.sot_for_cred_col or _detect_col(
        sot_keys, exacts=("businesspurpose_isforcredentialing","isforcredentialing","is_for_credentialing"),
        contains_all=("credential",), contains_any=("isfor","forcred","purpose"))
    gender_col = cfg.sot_gender_col or _detect_col(
        sot_keys, exacts=("gender","sex","providergender"), contains_any=("gender","sex"),
        avoid=("unisex",))
    progress(f"SOT identity cols — license:{lic_col or '—'} state:{lic_state_col or '—'} middle:{mid_col or '—'} "
             f"gender:{gender_col or '—'} for-credentialing:{forcred_col or '—'}  ·  NPDB gender:{npdb_gender_key or '—'}")

    def for_cred(p):
        """businessPurpose_isForCredentialing -> True (direct), False (delegated), or None (blank/absent)."""
        if not forcred_col: return None
        s = str(p.get(forcred_col, "")).strip().lower()
        if s == "": return None
        if s in ("true","yes","y","1","t","direct","x","checked"): return True
        if s in ("false","no","n","0","f","none","n/a","delegated"): return False
        return None

    providers, prov_lic = {}, defaultdict(set)
    for r in sot_raw:
        pid = str(r.get("providerId","")).strip()
        if not pid: continue
        if pid not in providers: providers[pid] = r
        if lic_col:
            fb_state = r.get(lic_state_col) if lic_state_col else ""
            prov_lic[pid] |= parse_sot_licenses(r.get(lic_col), fb_state)

    def _ratio(a, b):
        if not a or not b: return 0
        if a == b: return 100
        return fuzz.ratio(a, b) if fuzz else 0

    def _prov_ident(p):
        """Normalized identity for a SOT provider row (incl. its license set)."""
        pid = str(p.get("providerId","")).strip()
        return {"npi": npi_n(p.get("npi")), "ssn4": ssn4(p.get("ssn")), "dob": dob_n(p.get("dateOfBirth")),
                "first": name_n(p.get("firstName")), "last": name_n(p.get("lastName")),
                "gender": gender_n(p.get(gender_col)) if gender_col else "",
                "middle": name_n(p.get(mid_col)) if mid_col else "", "lic": prov_lic.get(pid, set())}

    def _score(pi, m):
        """Score identity `pi` against one NPDB record `m`. Returns (score, basis_list, last_ratio).
        Identity points: NPI 50, SSN-last4 30, DOB 20, license# 25; fuzzy last+first up to 25;
        middle name +5, license state +5, gender +5 (corroborators). Gender DISAGREEMENT (both
        present, M vs F) subtracts 10 — a weak negative that helps reject look-alike mismatches."""
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
        if pi["gender"] and m["gender"]:
            if pi["gender"] == m["gender"]: s += 5; b.append("gender")
            else: s -= 10; b.append("gender_mismatch")
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
        if "npi" in bb and ((best["last"] and pi["last"] and bln < 60)
                            or (best["dob"] and pi["dob"] and best["dob"] != pi["dob"])
                            or ("gender_mismatch" in bb)):
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
        # businessPurpose_isForCredentialing (when present) overrides: false = delegated provider
        # (no active enrollment wanted); true keeps the status-based expectation.
        cyc = str(p.get("credentialingCycle", "")).strip().lower()
        fc = for_cred(p)
        if cls == "terminated":
            expect = "terminated"
        elif fc is False:
            expect = "delegated"
        elif cls == "active" or cyc in cfg.recred_cycles:
            expect = "expects_active"
        else:
            expect = "in_progress"

        if expect == "expects_active":
            bucket = "EXP_MISSING" if n_enr == 0 else ("EXP_DUPLICATE" if n_enr > 1 else "EXP_OK")
        elif expect == "terminated":
            bucket = "TERM_SHOULD_CANCEL" if n_enr >= 1 else "TERM_OK"
        elif expect == "delegated":
            bucket = "DELEG_SHOULD_CANCEL" if n_enr >= 1 else "DELEG_OK"
        else:
            bucket = "IN_PROGRESS"
        acct[bucket] += 1; confc[conf] += 1

        flags = []
        if expect == "expects_active":
            if n_enr == 0:  flags.append("MISSING_ENROLLMENT")
            elif n_enr > 1: flags.append("DUPLICATE_ENROLLMENT")
        elif expect in ("terminated", "delegated") and n_enr >= 1:
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
    n_deleg  = acct["DELEG_OK"] + acct["DELEG_SHOULD_CANCEL"]
    n_prog   = acct["IN_PROGRESS"]
    total    = len(providers); tie = n_active + n_term + n_deleg + n_prog
    bal_ok   = (tie == total)
    action_total = len(action_all)        # distinct providers needing action
    sc_deleg = acct["DELEG_SHOULD_CANCEL"]
    def pct(x): return f"{round(100*x/total)}%" if (total and isinstance(x, int)) else ""

    # (style, label, value, note) — `style` drives Sheet formatting (see _summary_format_reqs).
    # 3 columns: Label | Count | %-of-total OR what-to-do. UIs render all three.
    SUMMARY_SPEC = [
        ("title", "NPDB Enrollment Reconciliation", "", ""),
        # ---- headline KPIs ----
        ("kpi", "Total providers", total, ""),
        (("bad" if action_total else "good"), "Need action", action_total, pct(action_total)),
        (("good" if bal_ok else "bad"), "Balanced?", "YES" if bal_ok else f"NO (diff {total-tie})", ""),
        ("blank", "", "", ""),
        # ---- action-first: only what needs work, with what-to-do ----
        ("colhdr", "ACTION NEEDED", "Count", "What to do"),
        ("warn", "Missing enrollment", acct["EXP_MISSING"], "enroll the provider in NPDB"),
        ("bad",  "Duplicate enrollment (>1 active)", acct["EXP_DUPLICATE"], "cancel the extras — keep the oldest"),
        ("bad",  "Should be cancelled — terminated", acct["TERM_SHOULD_CANCEL"], "cancel the active NPDB enrollment"),
    ]
    if forcred_col:
        SUMMARY_SPEC.append(
            ("bad", "Should be cancelled — delegated", sc_deleg, "cancel the active NPDB enrollment"))
    SUMMARY_SPEC += [
        ("warn", "Databank ID out of sync", len(db_updates), "fix the databank id stored in the SOT"),
        ("warn", "Identity conflicts", n_conflict, "review — NPI matched but identity differs"),
        ("blank", "", "", ""),
        # ---- full accounting ledger (buckets sum to total), with % of total ----
        ("colhdr", "FULL ACCOUNTING  (buckets sum to total)", "Providers", "% of total"),
        ("section", "EXPECTED active enrollment (active OR Recredentialing)", n_active, pct(n_active)),
        ("good",    "    OK — exactly 1 active enrollment", acct["EXP_OK"], pct(acct["EXP_OK"])),
        ("warn",    "    MISSING enrollment", acct["EXP_MISSING"], pct(acct["EXP_MISSING"])),
        ("sub",     "        of which: no NPDB record", miss_no_rec, ""),
        ("sub",     "        of which: NPDB record Canceled/Suspended", miss_inactive, ""),
        ("bad",     "    DUPLICATE enrollment (>1 active)", acct["EXP_DUPLICATE"], pct(acct["EXP_DUPLICATE"])),
        ("section", "TERMINATED / Cancelled / Denied", n_term, pct(n_term)),
        ("good",    "    OK — no active enrollment", acct["TERM_OK"], pct(acct["TERM_OK"])),
        ("bad",     "    SHOULD BE CANCELLED (still Enrolled)", acct["TERM_SHOULD_CANCEL"], pct(acct["TERM_SHOULD_CANCEL"])),
    ]
    if forcred_col:
        SUMMARY_SPEC += [
            ("section", "DELEGATED — not for credentialing", n_deleg, pct(n_deleg)),
            ("good",    "    OK — no active enrollment", acct["DELEG_OK"], pct(acct["DELEG_OK"])),
            ("bad",     "    SHOULD BE CANCELLED (still Enrolled)", sc_deleg, pct(sc_deleg)),
        ]
    SUMMARY_SPEC += [
        ("section", "IN PROGRESS / Other (not evaluated)", n_prog, pct(n_prog)),
        ("blank", "", "", ""),
        ("kpi", "Sum of buckets (must equal Total)", tie, ""),
        ("blank", "", "", ""),
        ("section", "Cross-cutting (overlaps the buckets above)", "", ""),
        ("normal",  "Databank ID needs update", len(db_updates), ""),
        ("sub",     "    missing in SOT (populate)", db_missing, ""),
        ("sub",     "    mismatch", db_mismatch, ""),
        ("warn",    "Identity conflicts (NPI matched, identity differs) — review", n_conflict, ""),
        ("blank", "", "", ""),
        ("section", "Extra NPDB enrollments NOT in SOT (reverse pass)", "", ""),
        ("sub",     "    distinct persons in NPDB with no SOT provider", len(extra_groups), ""),
        ("sub",     "        of which: high-confidence link to a provider", n_extra_link, ""),
        ("sub",     "        of which: not in SOT — suggest adding", n_extra_new, ""),
        ("sub",     "    (enrollment records behind them)", sum(len(v) for v in extra_groups.values()), ""),
        ("blank", "", "", ""),
        ("section", "Match confidence", "", ""),
        ("good",    "    HIGH", confc["HIGH"], pct(confc["HIGH"])),
        ("normal",  "    MEDIUM", confc["MEDIUM"], pct(confc["MEDIUM"])),
        ("warn",    "    LOW", confc["LOW"], pct(confc["LOW"])),
        ("sub",     "    UNMATCHED (no NPDB record)", confc["UNMATCHED"], pct(confc["UNMATCHED"])),
    ]
    summary = [[label, value, note] for _style, label, value, note in SUMMARY_SPEC]

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
        sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
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
        data["readme"] = _readme(cfg, sot_tab, npdb_tab, bool(sot_rows is not None))
        cap = cfg.max_rows_per_tab
        del_split = []
        for name in order:
            hdr = headers.get(name); rows = data[name]
            # split into <name>, <name>_2, _3 … when over the row cap (header repeats on each)
            if cap and len(rows) > cap:
                chunks = [((name if i == 0 else f"{name}_{i//cap+1}"), rows[i:i+cap])
                          for i in range(0, len(rows), cap)]
                progress(f"{name}: {len(rows):,} rows → {len(chunks)} tabs")
            else:
                chunks = [(name, rows)]
            titles = {t for t, _ in chunks}
            for title, chunk in chunks:
                if title not in existing:
                    resp = _retry(lambda t=title: svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id,
                        body={"requests":[{"addSheet":{"properties":{"title":t}}}]}).execute(), f"add {title}")
                    sheet_ids[title] = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
                    existing.add(title)
                _retry(lambda t=title: svc.spreadsheets().values().clear(spreadsheetId=sheet_id, range=f"'{t}'").execute(), f"clear {title}")
                body = ([hdr] + chunk) if hdr else chunk
                _retry(lambda t=title, b=body: svc.spreadsheets().values().update(spreadsheetId=sheet_id,
                    range=f"'{t}'!A1", valueInputOption="RAW", body={"values": b}).execute(), f"write {title}")
                written.append(title)
            # stale split tabs from a previous, larger run (<name>_N no longer produced)
            del_split += [t for t in list(existing)
                          if t not in titles and re.fullmatch(re.escape(name) + r"_\d+", t)]
        if del_split:
            _retry(lambda: svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests":
                [{"deleteSheet": {"sheetId": sheet_ids[t]}} for t in del_split if t in sheet_ids]}).execute(),
                "remove stale split tabs")
            existing -= set(del_split)

        # color-code & band the summary tab (values are already written above)
        if "summary" in sheet_ids:
            progress("Formatting summary tab…")
            _retry(lambda: svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id,
                body={"requests": _summary_format_reqs(sheet_ids["summary"], SUMMARY_SPEC)}).execute(),
                "format summary")

    return Result(total=total, balanced=(tie == total), action_count=len(action_all),
                  summary=summary, counts=dict(counts), confidence=dict(confc), written_tabs=written,
                  extra_enrollments=len(extra_groups))

def _summary_format_reqs(sid, spec):
    """Google-Sheets batchUpdate requests that turn the raw `summary` tab into a banded,
    color-coded report. `spec` is the (style, label, value, note) list built in reconcile()
    — 3 columns: Label | Count | %/what-to-do. Idempotent: clears prior formatting/borders."""
    def rgb(h):
        h = h.lstrip("#")
        return {"red": int(h[0:2],16)/255, "green": int(h[2:4],16)/255, "blue": int(h[4:6],16)/255}
    BLUE, MIDBLUE, LBLUE, GRAY = rgb("1f4e78"), rgb("2f6da3"), rgb("eaf1f8"), rgb("f1f4f9")
    WHITE, BLACK, MUTED, BORDER = rgb("ffffff"), rgb("202a36"), rgb("5a6573"), rgb("c9d3df")
    GBG, GFG = rgb("e6f4ea"), rgb("137333")   # green (OK)
    ABG, AFG = rgb("fef7e0"), rgb("8a5a00")   # amber (review / loose)
    RBG, RFG = rgb("fce8e6"), rgb("c5221f")   # red (action / out of balance)
    n = len(spec)
    NC = 3                                    # 3 columns
    full = {"sheetId": sid, "startRowIndex": 0, "endRowIndex": n, "startColumnIndex": 0, "endColumnIndex": NC}

    def fmt(bg=WHITE, fg=BLACK, bold=False, italic=False, size=10, halign="LEFT"):
        return {"backgroundColor": bg, "horizontalAlignment": halign, "verticalAlignment": "MIDDLE",
                "textFormat": {"bold": bold, "italic": italic, "foregroundColor": fg, "fontSize": size}}
    STYLE = {
        "blank":   fmt(),
        "normal":  fmt(),
        "sub":     fmt(fg=MUTED, italic=True),
        "section": fmt(bg=LBLUE, bold=True),
        "kpi":     fmt(bg=GRAY, bold=True, size=11),
        "good":    fmt(bg=GBG, fg=GFG, bold=True),
        "warn":    fmt(bg=ABG, fg=AFG, bold=True),
        "bad":     fmt(bg=RBG, fg=RFG, bold=True),
        "colhdr":  fmt(bg=MIDBLUE, fg=WHITE, bold=True),
        "title":   fmt(bg=BLUE, fg=WHITE, bold=True, size=13, halign="CENTER"),
    }
    reqs = [
        # column widths + clip long labels so each row stays a single line
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 460}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 90}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
            "properties": {"pixelSize": 300}, "fields": "pixelSize"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": n, "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP"}}, "fields": "userEnteredFormat.wrapStrategy"}},
        # wipe any borders left over from a previous run
        {"updateBorders": {"range": full, "top": {"style": "NONE"}, "bottom": {"style": "NONE"},
            "left": {"style": "NONE"}, "right": {"style": "NONE"},
            "innerHorizontal": {"style": "NONE"}, "innerVertical": {"style": "NONE"}}},
    ]
    # per-row look. A warn/bad count that is actually 0 means "nothing to do" -> show it green.
    for i, row in enumerate(spec):
        style, val = row[0], row[2]
        eff = style
        if style in ("warn", "bad") and isinstance(val, int) and not isinstance(val, bool) and val == 0:
            eff = "good"
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": i, "endRowIndex": i + 1, "startColumnIndex": 0, "endColumnIndex": NC},
            "cell": {"userEnteredFormat": STYLE[eff]},
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)"}})
    # count column (B): right-aligned, thousands separators, bold — applied after rows so it wins
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": n, "startColumnIndex": 1, "endColumnIndex": 2},
        "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT",
            "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}, "textFormat": {"bold": True}}},
        "fields": "userEnteredFormat(horizontalAlignment,numberFormat,textFormat.bold)"}})
    # note column (C): de-emphasise the %/what-to-do text (not bold), except in header rows
    colhdr_rows = {i for i, r in enumerate(spec) if r[0] == "colhdr"}
    for i in range(1, n):
        if i in colhdr_rows: continue
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": i, "endRowIndex": i + 1, "startColumnIndex": 2, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": False}}},
            "fields": "userEnteredFormat.textFormat.bold"}})
    # title banner spans all columns; outer box + accent rules; freeze the banner
    reqs.append({"mergeCells": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
        "startColumnIndex": 0, "endColumnIndex": NC}, "mergeType": "MERGE_ALL"}})
    reqs.append({"updateBorders": {"range": full,
        "top": {"style": "SOLID", "color": BORDER}, "bottom": {"style": "SOLID", "color": BORDER},
        "left": {"style": "SOLID", "color": BORDER}, "right": {"style": "SOLID", "color": BORDER}}})
    reqs.append({"updateBorders": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
        "startColumnIndex": 0, "endColumnIndex": NC}, "bottom": {"style": "SOLID_THICK", "color": MIDBLUE}}})
    for i in colhdr_rows:
        reqs.append({"updateBorders": {"range": {"sheetId": sid, "startRowIndex": i, "endRowIndex": i + 1,
            "startColumnIndex": 0, "endColumnIndex": NC}, "bottom": {"style": "SOLID_MEDIUM", "color": MIDBLUE}}})
    reqs.append({"updateSheetProperties": {"properties": {"sheetId": sid,
        "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}})
    return reqs

def _readme(cfg, sot_tab, npdb_tab, sot_from_bq=False):
    sot_src = "pulled from BigQuery for the selected client" if sot_from_bq \
              else f"read from the '{sot_tab}' tab"
    return [[x] for x in [
        "NPDB ENROLLMENT RECONCILIATION",
        "",
        "PURPOSE",
        "  Each provider we credential should hold one active NPDB enrollment; terminated and delegated",
        "  providers should hold none. This report reconciles every provider's status against their NPDB",
        "  enrollment for the selected client and lists the discrepancies for us to action.",
        "",
        "INPUTS",
        f"  • SOT (our records) — {sot_src}: one row per provider.",
        f"  • NPDB report — the '{npdb_tab}' tab of this sheet: one row per NPDB enrollment.",
        "  Each provider is matched to their NPDB record on multiple identity fields (see Methodology).",
        "",
        "RESULT TABS",
        "  summary              Headline figures and the actions required.",
        "  missing_enrollment   Should be enrolled but is not (or not active) — we enroll the provider.",
        "  duplicates           More than one active enrollment — we cancel the extras, keeping the oldest.",
        "  should_be_cancelled  Terminated or delegated but still enrolled — we cancel the enrollment.",
        "  databank_updates     The databank id on record is missing or incorrect — we correct it in the SOT.",
        "  extra_enrollments    NPDB enrollments with no matching provider — we link them or add the provider.",
        "  reconciliation       Full line-by-line result for every provider (audit trail).",
        "",
        "NOTES",
        "  • This is a report; it does not modify NPDB or any system. We action the items.",
        "  • Re-running overwrites these tabs. Retain a copy beforehand if needed.",
        "  • Matching uses full identity (NPI with name, DOB, license and gender), never NPI alone.",
        "    Uncertain matches are flagged under 'identity conflicts' for review.",
        "  • Providers still in progress are not reported as missing.",
        "  • Delegated handling applies only when the client's data includes the for-credentialing flag.",
        "  • Gender and SSN are sourced from CAQH; where absent, they are not used in matching.",
        "",
        "METHODOLOGY",
        "  Our status -> meaning:",
        f"    Active     : {', '.join(sorted(cfg.active_statuses))}",
        f"    Terminated : {', '.join(sorted(cfg.terminated_statuses))}",
        "    In progress: not evaluated for missing/duplicate. Recredentialing also expects an active enrollment.",
        "    businessPurpose_isForCredentialing: true = direct (expect enrollment); false = delegated (none).",
        "  NPDB status -> meaning:",
        f"    Enrolled (active): {', '.join(sorted(cfg.npdb_active))}; Cancelled: {', '.join(sorted(cfg.npdb_cancelled))}.",
        "  Identity match (points): NPI +50, SSN-last4 +30, DOB +20, license# +25, name (fuzzy), middle/state/gender +5;",
        f"    gender disagreement -10. Requires an anchor; minimum accept score {int(cfg.accept_score)}. Confidence HIGH/MEDIUM/LOW.",
        "  Accounting buckets are mutually exclusive and sum to the provider total. Databank fixes, identity",
        "  conflicts and extra enrollments are counted separately. Large tabs split into <name>_2, _3, ….",
        "",
        f"  Access: share this sheet (Editor) with {SA_EMAIL}.",
    ]]
