import { useEffect, useState } from "react";

const api = (path, opts) => fetch(path, opts).then(async (r) => {
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
});

export default function App() {
  const [saEmail, setSaEmail] = useState("");
  const [sheet, setSheet] = useState("");
  const [tabs, setTabs] = useState([]);
  const [npdb, setNpdb] = useState("");
  const [clients, setClients] = useState([]);
  const [client, setClient] = useState("");
  const [loadingTabs, setLoadingTabs] = useState(false);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState(null);
  const [accept, setAccept] = useState(45);
  const [bqSql, setBqSql] = useState("");
  const [mode, setMode] = useState("sheet");   // NPDB source: "sheet" tab | "bq" query
  const [npdbSql, setNpdbSql] = useState("");

  useEffect(() => { api("/api/info").then((d) => setSaEmail(d.sa_email)).catch(() => {}); }, []);
  useEffect(() => {
    api("/api/clients")
      .then((d) => setClients(d.clients || []))
      .catch((e) => setErr(e.message));
  }, []);

  const loadTabs = async () => {
    setErr(""); setResult(null); setTabs([]); setLoadingTabs(true);
    try {
      const d = await api(`/api/tabs?sheet=${encodeURIComponent(sheet)}`);
      setTabs(d.tabs);
      setNpdb(d.tabs.find((t) => t.toLowerCase().includes("npdb")) || d.tabs[0] || "");
    } catch (e) { setErr(e.message); }
    setLoadingTabs(false);
  };

  const run = async () => {
    setErr(""); setResult(null); setRunning(true);
    try {
      const d = await api("/api/reconcile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sheet, client, accept_score: accept,
                               npdb_tab: mode === "sheet" ? npdb : undefined,
                               npdb_sql: mode === "bq" ? npdbSql.trim() : undefined,
                               bq_sql: bqSql.trim() || undefined }),
      });
      setResult(d);
    } catch (e) { setErr(e.message); }
    setRunning(false);
  };

  const sheetUrl = result ? `https://docs.google.com/spreadsheets/d/${result.sheet_id}` : "#";

  return (
    <div className="wrap">
      <h1>🩺 NPDB Enrollment Reconciliation</h1>
      <p className="sub">Reconcile credentialing status (SOT) vs NPDB enrollment status — per client.</p>

      <div className="card banner">
        <b>Step 1 — share your Google Sheet (Editor) with:</b>
        <div className="sa">
          <code>{saEmail || "…"}</code>
          <button onClick={() => navigator.clipboard.writeText(saEmail)}>Copy</button>
        </div>
      </div>

      <div className="card">
        <label>Step 2 — NPDB report source</label>
        <div className="seg">
          <button type="button" className={mode === "sheet" ? "on" : ""} onClick={() => setMode("sheet")}>Google Sheet</button>
          <button type="button" className={mode === "bq" ? "on" : ""} onClick={() => setMode("bq")}>BigQuery query</button>
        </div>

        {mode === "sheet" && (
          <>
            <label>Google Sheet URL or ID (holds the NPDB tab; results are written here)</label>
            <div className="row">
              <input value={sheet} onChange={(e) => setSheet(e.target.value)} placeholder="https://docs.google.com/spreadsheets/d/…" />
              <button onClick={loadTabs} disabled={!sheet || loadingTabs}>{loadingTabs ? "Loading…" : "Load tabs"}</button>
            </div>

            {tabs.length > 0 && (
              <div className="row two">
                <div>
                  <label>NPDB tab (enrollments)</label>
                  <select value={npdb} onChange={(e) => setNpdb(e.target.value)}>
                    {tabs.map((t) => <option key={t}>{t}</option>)}
                  </select>
                </div>
                <div>
                  <label>Client / organization (SOT from BigQuery)</label>
                  <select value={client} onChange={(e) => setClient(e.target.value)}>
                    <option value="">{clients.length ? "Pick a client…" : "Loading…"}</option>
                    {clients.map((c) => <option key={c}>{c}</option>)}
                  </select>
                </div>
              </div>
            )}
          </>
        )}

        {mode === "bq" && (
          <>
            <label>BigQuery SQL — NPDB report (one row per enrollment)</label>
            <textarea
              value={npdbSql}
              onChange={(e) => setNpdbSql(e.target.value)}
              rows={7}
              spellCheck={false}
              placeholder={"SELECT first_name, last_name, npi, ssn, birthdate, license,\n       npdb_enrollment_status, data_bank_subject_id_number, …\nFROM `project.dataset.npdb_report`"}
            />
            <small>
              Single SELECT returning the NPDB report columns (First Name, Last Name, NPI, SSN,
              Birthdate, License, NPDB Enrollment Status, Data Bank Subject ID Number, …).
              BigQuery-style names (<code>first_name</code>, <code>npdb_enrollment_status</code>…)
              are mapped automatically. Use <b>@client</b> to filter by the selected client.
            </small>

            <label>Results sheet — Google Sheet URL or ID (result tabs are written here)</label>
            <input value={sheet} onChange={(e) => setSheet(e.target.value)} placeholder="https://docs.google.com/spreadsheets/d/…" />
            <small>Share it (Editor) with the service account above.</small>

            <label>Client / organization (SOT from BigQuery)</label>
            <select value={client} onChange={(e) => setClient(e.target.value)}>
              <option value="">{clients.length ? "Pick a client…" : "Loading…"}</option>
              {clients.map((c) => <option key={c}>{c}</option>)}
            </select>
          </>
        )}

        {(mode === "bq" || tabs.length > 0) && (
          <>
            <details className="adv" open={!!bqSql.trim()}>
              <summary>Optional — custom SOT query (BigQuery SQL)</summary>
              <textarea
                value={bqSql}
                onChange={(e) => setBqSql(e.target.value)}
                rows={6}
                spellCheck={false}
                placeholder={"SELECT providerId, firstName, lastName, npi, dateOfBirth, credentialingStatus, …\nFROM `project.dataset.table`\nWHERE organization = @client"}
              />
              <small>
                Overrides the built-in SOT query. Must be a single SELECT returning at least a
                {" "}<b>providerId</b> column (plus firstName, lastName, npi, dateOfBirth,
                credentialingStatus…). Use <b>@client</b> to filter by the selected client;
                without it the client pick isn&apos;t needed.
              </small>
            </details>

            <details className="adv">
              <summary>Advanced — matching</summary>
              <label>Match accept score: <b>{accept}</b></label>
              <input type="range" min="30" max="80" value={accept} onChange={(e) => setAccept(+e.target.value)} />
              <small>Lower = more matches (looser); higher = stricter.</small>
            </details>
          </>
        )}

        <button className="run" onClick={run}
          disabled={running || !sheet
            || (mode === "sheet" ? !npdb : !npdbSql.trim())
            || ((!bqSql.trim() || bqSql.includes("@client") || (mode === "bq" && npdbSql.includes("@client"))) && !client)}>
          {running ? "Running… (queries BigQuery, matches, writes results)" : "▶  Run Reconciliation"}
        </button>
      </div>

      {err && <div className="card error">⚠️ {err}</div>}

      {result && (
        <div className="card result">
          <div className="hd">
            <span>✅ {result.total.toLocaleString()} providers · {result.action_count.toLocaleString()} need action ·
              {" "}Balanced: {result.balanced ? "YES ✅" : "NO ⚠️"}</span>
            <a href={sheetUrl} target="_blank" rel="noreferrer">Open Sheet ↗</a>
          </div>
          <table>
            <tbody>
              {result.summary.filter((r) => (r[0] || "") !== "" || (r[1] || "") !== "").map((r, i) => (
                <tr key={i} className={String(r[0]).startsWith("    ") ? "indent" : ""}>
                  <td>{r[0]}</td>
                  <td className="num">{r[1] !== "" ? r[1] : ""}</td>
                  <td className="note">{r[2] || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="tabs">Tabs written: {result.written_tabs.join(" · ")}</p>
        </div>
      )}

      <footer>PHI — internal use only.</footer>
    </div>
  );
}
