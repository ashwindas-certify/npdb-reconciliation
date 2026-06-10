"""CLI wrapper around reconcile_core — same logic, runnable headless / schedulable.

  python reconcile_cli.py --sheet <id_or_url> --sot SOT --npdb "NPDB Report"
"""
import argparse, re, sys
from reconcile_core import Config, reconcile

def sheet_id(s):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", s or "")
    return m.group(1) if m else s

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", required=True, help="Sheet URL or ID")
    ap.add_argument("--sot", default="SOT")
    ap.add_argument("--npdb", default="NPDB Report")
    ap.add_argument("--accept", type=float, default=45.0)
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()
    res = reconcile(sheet_id(a.sheet), a.sot, a.npdb, Config(accept_score=a.accept),
                    write=not a.no_write, progress=lambda m: print(m))
    print(f"\nTotal {res.total:,} | action {res.action_count:,} | "
          f"extra NPDB enrollments not in SOT {res.extra_enrollments:,} | balanced {res.balanced}")
    print("Confidence:", res.confidence)
    print("Wrote tabs:", ", ".join(res.written_tabs) or "(none)")

if __name__ == "__main__":
    sys.exit(main())
