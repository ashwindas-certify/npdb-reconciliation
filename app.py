"""NPDB Enrollment Reconciliation — Streamlit front end.

Run locally:   streamlit run app.py
The user shares their Google Sheet with the service account, then either pastes the
sheet URL and picks the NPDB tab, OR pastes a BigQuery query that returns the NPDB
report (plus a results sheet). Results are written back as tabs and the accounting
summary is shown.
"""
import re
import pandas as pd
import streamlit as st
from reconcile_core import (Config, get_service, list_tabs, reconcile, bq_sot, bq_rows,
                            bq_clients, SA_EMAIL, DEFAULT_BQ_PROJECT)

st.set_page_config(page_title="NPDB Reconciliation", page_icon="🩺", layout="centered")
st.title("🩺 NPDB Enrollment Reconciliation")
st.caption("Reconcile credentialing status (SOT) vs NPDB enrollment status — per client.")

def sheet_id_from(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url or "")
    return m.group(1) if m else (url or "").strip()

# 1) share-with-SA banner
st.info(f"**Step 1 — share your Google Sheet (Editor) with:**\n\n`{SA_EMAIL}`")

# 2) NPDB report source — a sheet tab OR a custom BigQuery query
mode = st.radio("Step 2 — NPDB report source", ["Google Sheet tab", "BigQuery query"], horizontal=True)
sot_tab = npdb_tab = None
npdb_sql = ""
tabs = []

if mode == "Google Sheet tab":
    url = st.text_input("Google Sheet URL or ID (holds the NPDB tab; results are written here)")
else:
    npdb_sql = st.text_area(
        "BigQuery SQL — NPDB report (one row per enrollment)", height=160,
        value=("SELECT * FROM `certifyos-production-platform.npdb_exports_test.practitioner_enrollments`\n"
               "WHERE submitted_on_behalf_of_entity = 'NPDB Entity Name'"))
    st.caption("Single SELECT returning the NPDB report columns. **Replace `'NPDB Entity Name'` with the client's "
               "NPDB entity** (or edit the query as needed). `SELECT *` is fine — BigQuery-style names "
               "(`first_name`, `npdb_enrollment_status`, `submitted_on_behalf_of_entity`…) are mapped automatically. "
               "Use `@client` to filter by the selected client.")
    url = st.text_input("Results sheet — Google Sheet URL or ID (result tabs are written here)")

sid = sheet_id_from(url)
if sid:
    try:
        svc = get_service()
        tabs = list_tabs(svc, sid)
        st.success(f"Connected ✓ — {len(tabs)} tabs found")
    except Exception as e:
        msg = str(e)
        if "403" in msg or "permission" in msg.lower() or "not have" in msg.lower():
            st.error("The service account can't open this sheet. Share it (Editor) with the address above, then retry.")
        elif "404" in msg or "not found" in msg.lower():
            st.error("Sheet not found — check the URL/ID.")
        else:
            st.error(f"Couldn't open the sheet: {msg[:200]}")
        tabs = []

if mode == "Google Sheet tab" and tabs:
    def _guess(names, want):
        for n in names:
            if want in n.lower(): return names.index(n)
        return 0
    npdb_tab = st.selectbox("NPDB tab (enrollments) — in the sheet above", tabs, index=_guess(tabs, "npdb"))

# 2b) client (SOT comes from BigQuery, filtered by organization name)
@st.cache_data(show_spinner="Loading clients from BigQuery…")
def _load_clients():
    return bq_clients(Config())

client = None
try:
    _clients = _load_clients()
    client = st.selectbox("Step 3 — Client / organization (SOT from BigQuery)", _clients,
                          index=None, placeholder="Pick a client…")
except Exception as e:
    msg = str(e)
    if "default credentials" in msg.lower() or "reauth" in msg.lower():
        st.error("Not logged in to BigQuery. In a terminal run:  `gcloud auth application-default login`")
    else:
        st.error(f"Couldn't load clients from BigQuery: {msg[:200]}")

# 3) advanced config
with st.expander("Advanced — status mappings & matching (optional)"):
    cfg_default = Config()
    active_txt = st.text_area("ACTIVE credentialingStatus values (one per line)",
                              "\n".join(sorted(cfg_default.active_statuses)))
    term_txt   = st.text_area("TERMINATED / Cancelled / Denied values (one per line)",
                              "\n".join(sorted(cfg_default.terminated_statuses)))
    npdb_active_txt = st.text_input("NPDB 'active' enrollment statuses (comma-sep)",
                                    ", ".join(sorted(cfg_default.npdb_active)))
    npdb_cancel_txt = st.text_input("NPDB 'cancelled' statuses (comma-sep)",
                                    ", ".join(sorted(cfg_default.npdb_cancelled)))
    accept = st.slider("Match accept score", 30, 80, int(cfg_default.accept_score),
                       help="Lower = more matches (looser); higher = stricter.")
    sot_sql_txt = st.text_area(
        "Custom SOT query (BigQuery SQL) — optional, overrides the built-in SOT query", "", height=140,
        placeholder=("SELECT providerId, firstName, lastName, npi, dateOfBirth, credentialingStatus, …\n"
                     "FROM `project.dataset.table`\nWHERE organization = @client"),
        help="Must be a single SELECT returning at least a providerId column (plus firstName, lastName, "
             "npi, dateOfBirth, credentialingStatus…). Use @client to filter by the selected client; "
             "without it the client pick isn't needed.")

def build_cfg():
    return Config(
        active_statuses={s.strip().lower() for s in active_txt.splitlines() if s.strip()},
        terminated_statuses={s.strip().lower() for s in term_txt.splitlines() if s.strip()},
        npdb_active={s.strip().lower() for s in npdb_active_txt.split(",") if s.strip()},
        npdb_cancelled={s.strip().lower() for s in npdb_cancel_txt.split(",") if s.strip()},
        accept_score=float(accept),
    )

# 4) run
st.divider()
_clean = lambda s: (s or "").strip().rstrip(";").strip()
sot_sql, npdb_q = _clean(sot_sql_txt), _clean(npdb_sql)
# a client is needed unless every BigQuery source is a custom query that doesn't use @client
needs_client = (not sot_sql) or ("@client" in sot_sql) or ("@client" in npdb_q)
ready = bool(sid) and bool(npdb_tab if mode == "Google Sheet tab" else npdb_q) \
        and (bool(client) or not needs_client)
make_client_wb = st.checkbox(
    "Also create a separate client-facing workbook (summary + recon page), shared anyone-with-link",
    value=False, help="A clean spreadsheet with just the client summary (charts) and a trimmed reconciliation page. "
                      "The full detail tabs are still written to the results sheet above.")
run = st.button("▶  Run Reconciliation", type="primary", disabled=not ready)
if run:
    for label, q in (("SOT", sot_sql), ("NPDB", npdb_q)):
        if q and not re.match(r"(?is)^(with|select)\b", q):
            st.error(f"The {label} query must be a single SELECT (or WITH … SELECT) statement.")
            st.stop()
    status = st.empty()
    with st.spinner("Running…"):
        try:
            cfg = build_cfg()
            _tick = lambda m: status.write(f"⏳ {m}")
            if sot_sql:
                status.write("⏳ Querying BigQuery — custom SOT query…")
                sot_rows = bq_rows(sot_sql, {"client": client} if "@client" in sot_sql else None,
                                   project=DEFAULT_BQ_PROJECT, progress=_tick)
                if not sot_rows:
                    raise ValueError("The custom SOT query returned no rows.")
                if "providerId" not in sot_rows[0]:
                    raise ValueError("The custom SOT query must return a providerId column "
                                     "(plus firstName, lastName, npi, dateOfBirth, credentialingStatus, …).")
            else:
                status.write(f"⏳ Querying BigQuery for {client}…")
                sot_rows = bq_sot(client, cfg, progress=_tick)
            npdb_rows = None
            if npdb_q:
                status.write("⏳ Querying BigQuery — NPDB report…")
                npdb_rows = bq_rows(npdb_q, {"client": client} if "@client" in npdb_q else None,
                                    project=DEFAULT_BQ_PROJECT, progress=_tick)
                if not npdb_rows:
                    raise ValueError("The NPDB query returned no rows.")
            cw_title = f"NPDB Reconciliation — {client}" if client else "NPDB Enrollment — Client Summary"
            res = reconcile(sid, None, npdb_tab, cfg, write=True, sot_rows=sot_rows, npdb_rows=npdb_rows,
                            progress=lambda m: status.write(f"⏳ {m}"),
                            client_workbook=make_client_wb, client_workbook_title=cw_title)
        except Exception as e:
            msg = str(e)
            if "default credentials" in msg.lower() or "reauth" in msg.lower():
                st.error("Not logged in to BigQuery. Run:  `gcloud auth application-default login`")
            else:
                st.error(f"Run failed: {msg[:300]}")
            st.stop()
    status.empty()
    st.success(f"Done ✓ — {res.total:,} providers · {res.action_count:,} need action · "
               f"{res.extra_enrollments:,} extra NPDB enrollments not in SOT · "
               f"Balanced: {'YES ✅' if res.balanced else 'NO ⚠️'}")
    st.markdown(f"**Results written to:** {', '.join('`'+t+'`' for t in res.written_tabs)}")
    st.link_button("Open Google Sheet ↗", f"https://docs.google.com/spreadsheets/d/{sid}")
    if res.client_workbook_url:
        st.success("Client-facing workbook created (anyone with the link can view).")
        st.link_button("Open client workbook ↗", res.client_workbook_url)
    elif make_client_wb:
        st.warning("The client workbook couldn't be created — check the run log above (Drive API/scope).")

    # summary table (Label | Count | %/what-to-do)
    df = pd.DataFrame([r for r in res.summary if any(str(c).strip() for c in r)],
                      columns=["", "Count", "What to do / %"])
    st.subheader("Summary")
    st.dataframe(df, use_container_width=True, hide_index=True)
