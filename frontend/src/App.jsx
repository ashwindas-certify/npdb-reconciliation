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
        body: JSON.stringify({ sheet, npdb_tab: npdb, client, accept_score: accept }),
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
        <label>Step 2 — NPDB report: Google Sheet URL or ID</label>
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

        {tabs.length > 0 && (
          <details className="adv">
            <summary>Advanced — matching</summary>
            <label>Match accept score: <b>{accept}</b></label>
            <input type="range" min="30" max="80" value={accept} onChange={(e) => setAccept(+e.target.value)} />
            <small>Lower = more matches (looser); higher = stricter.</small>
          </details>
        )}

        <button className="run" onClick={run} disabled={!client || !npdb || running}>
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
