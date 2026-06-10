"""NPDB Enrollment Reconciliation — Streamlit front end.

Run locally:   streamlit run app.py
The user shares their Google Sheet with the service account, pastes the sheet URL,
picks the SOT + NPDB tabs, and clicks Run. Results are written back as tabs and the
accounting summary is shown.
"""
import re
import pandas as pd
import streamlit as st
from reconcile_core import Config, get_service, list_tabs, reconcile, SA_EMAIL

st.set_page_config(page_title="NPDB Reconciliation", page_icon="🩺", layout="centered")
st.title("🩺 NPDB Enrollment Reconciliation")
st.caption("Reconcile credentialing status (SOT) vs NPDB enrollment status — per client.")

def sheet_id_from(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url or "")
    return m.group(1) if m else (url or "").strip()

# 1) share-with-SA banner
st.info(f"**Step 1 — share your Google Sheet (Editor) with:**\n\n`{SA_EMAIL}`")

# 2) sheet + tabs
url = st.text_input("Step 2 — Google Sheet URL or ID")
sid = sheet_id_from(url)
sot_tab = npdb_tab = None
tabs = []
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

if tabs:
    c1, c2 = st.columns(2)
    def _guess(names, want):
        for n in names:
            if want in n.lower(): return names.index(n)
        return 0
    sot_tab  = c1.selectbox("SOT tab (providers)", tabs, index=_guess(tabs, "sot"))
    npdb_tab = c2.selectbox("NPDB tab (enrollments)", tabs, index=_guess(tabs, "npdb"))

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
run = st.button("▶  Run Reconciliation", type="primary", disabled=not (sid and sot_tab and npdb_tab))
if run:
    status = st.empty()
    with st.spinner("Running…"):
        try:
            res = reconcile(sid, sot_tab, npdb_tab, build_cfg(), write=True,
                            progress=lambda m: status.write(f"⏳ {m}"))
        except Exception as e:
            st.error(f"Run failed: {str(e)[:300]}"); st.stop()
    status.empty()
    st.success(f"Done ✓ — {res.total:,} providers · {res.action_count:,} need action · "
               f"{res.extra_enrollments:,} extra NPDB enrollments not in SOT · "
               f"Balanced: {'YES ✅' if res.balanced else 'NO ⚠️'}")
    st.markdown(f"**Results written to:** {', '.join('`'+t+'`' for t in res.written_tabs)}")
    st.link_button("Open Google Sheet ↗", f"https://docs.google.com/spreadsheets/d/{sid}")

    # accounting table
    df = pd.DataFrame([r for r in res.summary if any(str(c).strip() for c in r)], columns=["Bucket","Providers"])
    st.subheader("Summary (accounting)")
    st.dataframe(df, use_container_width=True, hide_index=True)
