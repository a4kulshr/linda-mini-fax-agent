"""Worker entry point: python -m linda_agent [--fresh] [--headed]

Phases: inventory + download + OCR in session A, decide, then route each fax in its own fresh
session B, then a final verification pass in another fresh session. State is checkpointed to
runs/state.json after every step so a killed run resumes without resubmitting.
"""
import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PWError, sync_playwright

from . import portal
from .ocr import ocr_pdf, validate_pdf
from .rules import decide, extract

RUNS = Path(os.getenv("LINDA_RUNS", "runs"))
STATE = RUNS / "state.json"
DOWNLOADS = RUNS / "downloads"
MAX_ATTEMPTS = 3
CRASH_AT = os.getenv("LINDA_AGENT_CRASH_AT", "")  # test hook: before_submit | after_submit


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def save(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, STATE)


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def crash(point):
    if CRASH_AT == point:
        log(f"LINDA_AGENT_CRASH_AT={point}: exiting abruptly")
        os._exit(70)


def fields_match(observed: dict, fields: dict) -> bool:
    return all(observed.get(k) == fields[k] for k in ("assignee", "doc_type", "folder"))


# ---------- phase 1+2: session A ----------

def reconcile_inventory(state, inv):
    for row in inv:
        rec = state["faxes"].setdefault(row["fax_id"], {"fax_id": row["fax_id"], "errors": [], "attempts": []})
        done = rec.get("final", {}).get("action") in ("routed", "needs_review")
        if done and row["status"] == "New":
            sys.exit(f"{row['fax_id']}: state says we routed it but the portal shows New. "
                     "The sandbox was probably reset; rerun with --fresh.")
        rec.update({k: row[k] for k in ("received_at", "sender", "ui_pages")})
        rec["status"] = row["status"]


def download_and_extract(sess, rec):
    fid = rec["fax_id"]
    dest = DOWNLOADS / f"{fid}.pdf"
    if rec.get("sha256") and dest.exists() and sha256(dest) == rec["sha256"] and rec.get("evidence"):
        return True
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            tmp = sess.download(fid, dest)
            validate_pdf(tmp, rec["ui_pages"])
            os.replace(tmp, dest)
            break
        except (portal.PortalError, ValueError, PWError) as e:
            rec["errors"].append({"phase": "download", "attempt": attempt, "error": str(e)[:300]})
            log(f"{fid}: download attempt {attempt} failed: {e}")
            dest.with_suffix(".part").unlink(missing_ok=True)
            time.sleep(attempt)
    else:
        return False
    rec["sha256"] = sha256(dest)
    rec["downloaded_in"] = sess.name
    pages = ocr_pdf(dest)
    rec["ocr"] = [{k: p[k] for k in ("page", "confidence", "readable")} for p in pages]
    rec["evidence"] = extract(fid, pages)
    log(f"{fid}: downloaded + OCR'd {len(pages)} page(s), categories={rec['evidence']['categories']}")
    return True


# ---------- phase 4: one fresh session B per fax ----------

def route_one(browser, state, rec, redownload):
    fid, d = rec["fax_id"], rec["decision"]
    resumed = "submitting" in rec
    for attempt in range(1, MAX_ATTEMPTS + 1):
        sess = None
        try:
            sess = portal.Session(browser, f"B:{fid}:{attempt}")
            st = sess.read_status(fid)
            if st["status"] == "Routed":
                return finish_routed(sess, rec, st, resumed or bool(rec["attempts"]))
            rec["submitting"] = {"attempt": attempt, "at": time.time(), "session": sess.name}
            save(state)

            def before_ok():
                crash("before_submit")
            shown = sess.submit_route(fid, d["fields"], d["note"], before_ok)
            rec["attempts"].append({"attempt": attempt, "session": sess.name, "page_after_submit": shown[:200]})
            save(state)
            crash("after_submit")
            log(f"{fid}: submit attempt {attempt} -> {shown[:80]!r}; re-reading status")
            if "separate login session" in shown:
                redownload(rec)
            st = sess.read_status(fid)  # fresh navigation; re-login handled inside
            if st["status"] == "Routed":
                return finish_routed(sess, rec, st, recovered=shown != "Fax routing was recorded successfully.")
            rec["errors"].append({"phase": "route", "attempt": attempt, "error": f"still New after submit ({shown[:120]})"})
        except (portal.PortalError, PWError) as e:
            rec["errors"].append({"phase": "route", "attempt": attempt, "error": str(e)[:300]})
            log(f"{fid}: route attempt {attempt} failed: {e}")
        finally:
            if sess:
                rec.setdefault("sessions", []).append({"name": sess.name, "logins": sess.logins})
                sess.close()
        time.sleep(attempt)
    rec["final"] = {"action": "outcome_unknown" if "submitting" in rec else "failed",
                    "verification": "unverified"}
    return rec["final"]


def finish_routed(sess, rec, observed, recovered):
    d = rec["decision"]
    queue = sess.routed_documents().get(rec["fax_id"])
    rec["observed"] = {k: observed[k] for k in ("assignee", "doc_type", "folder")}
    ok = fields_match(observed, d["fields"]) and queue is not None and fields_match(queue, d["fields"])
    if not ok:
        rec["final"] = {"action": "conflict", "verification": "mismatch",
                        "detail": f"portal shows {observed}, routed-docs row {queue}, we intended {d['fields']}"}
    elif not recovered and "submitting" not in rec:
        rec["final"] = {"action": "conflict", "verification": "mismatch",
                        "detail": "fax was routed by someone else before we submitted"}
    else:
        rec["final"] = {"action": d["action"] if d["action"] == "needs_review" else "routed",
                        "verification": "recovered" if recovered else "verified",
                        "detail": "fax detail + Routed Documents show the intended values (notes are not shown by the UI)"}
    return rec["final"]


# ---------- results ----------

def write_results(state):
    rows = []
    for fid in sorted(state["faxes"]):
        r = state["faxes"][fid]
        ev, d, fin = r.get("evidence") or {}, r.get("decision") or {}, r.get("final") or {}
        rows.append({
            "fax_id": fid, "received_at": r.get("received_at"), "sender": r.get("sender"),
            "pages_ui": r.get("ui_pages"), "pdf_sha256": r.get("sha256"), "ocr_pages": r.get("ocr"),
            "extracted_category": d.get("category"), "page_titles": [(p["page"], p["title"]) for p in ev.get("pages", [])],
            "source_id": ev.get("source_id"), "identifiers": ev.get("ids"), "patient": ev.get("patient"), "dob": ev.get("dob"),
            "flags": ev.get("flags"), "review_reasons": d.get("reasons"), "duplicate_of": d.get("duplicate_of"),
            "related_faxes": d.get("related"), "action": fin.get("action"), "selected_fields": d.get("fields"),
            "note": d.get("note"), "observed_fields": r.get("observed"), "verification": fin.get("verification"),
            "verification_detail": fin.get("detail"), "submit_attempts": r.get("attempts"),
            "sessions": r.get("sessions"), "errors": r.get("errors"),
        })
    (RUNS / "results.json").write_text(json.dumps({"summary": summary(rows), "faxes": rows}, indent=1))
    flat = ["fax_id", "received_at", "extracted_category", "source_id", "action", "duplicate_of", "verification"]
    with open(RUNS / "results.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(flat + ["assignee", "doc_type", "folder", "note", "retries", "errors"])
        for x in rows:
            f = x["selected_fields"] or {}
            w.writerow([x[k] for k in flat] + [f.get("assignee"), f.get("doc_type"), f.get("folder"), x["note"],
                        len(x["submit_attempts"] or []), " | ".join(e["error"] for e in x["errors"] or [])])
    return rows


def summary(rows):
    out = {}
    for r in rows:
        out[r["action"] or "pending"] = out.get(r["action"] or "pending", 0) + 1
    return out


# ---------- main ----------

def run(fresh=False, headed=False):
    if fresh and RUNS.exists():
        shutil.rmtree(RUNS)
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {"faxes": {}}
    if state["faxes"]:
        log(f"resuming from {STATE} ({len(state['faxes'])} faxes)")
    portal.wait_ready()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)

        def session_a(name="A"):
            s = portal.Session(browser, name)
            log(f"session {name}: logged in")
            return s

        a = session_a()
        inv = a.inventory()
        log(f"inventory: {len(inv)} faxes")
        reconcile_inventory(state, inv)
        save(state)
        for fid in sorted(state["faxes"]):
            rec = state["faxes"][fid]
            if rec.get("final"):
                continue
            if not download_and_extract(a, rec):
                rec["final"] = {"action": "failed", "verification": "unverified",
                                "detail": f"no valid PDF after {MAX_ATTEMPTS} download attempts"}
            save(state)
        a.close()

        # Decisions are frozen once made: on resume our own routes show as "Routed" and must not
        # be re-decided as already_routed.
        readable = [r for r in state["faxes"].values() if r.get("evidence")]
        for fid, d in decide(readable).items():
            rec = state["faxes"][fid]
            if not rec.get("final") and "decision" not in rec:
                rec["decision"] = d
        save(state)

        def redownload(rec):
            s = session_a(f"A-redownload:{rec['fax_id']}")
            try:
                rec.pop("sha256", None)
                download_and_extract(s, rec)
            finally:
                s.close()

        for fid in sorted(state["faxes"]):
            rec = state["faxes"][fid]
            d = rec.get("decision")
            if rec.get("final") or not d:
                continue
            if d["action"] in ("route", "needs_review"):
                fin = route_one(browser, state, rec, redownload)
            else:
                rec["final"] = {"action": d["action"], "verification": "not_submitted"}
                fin = rec["final"]
            log(f"{fid}: {fin['action']} ({fin['verification']})")
            save(state)

        # Independent check in a fresh session: every route we claim exists, nothing we skipped got routed.
        v = portal.Session(browser, "verify")
        queue = v.routed_documents()
        for fid, rec in state["faxes"].items():
            fin = rec.get("final", {})
            if fin.get("action") in ("routed", "needs_review"):
                fin["in_routed_documents"] = fid in queue and fields_match(queue[fid], rec["decision"]["fields"])
            elif fin.get("action") == "skipped_duplicate":
                fin["in_routed_documents"] = fid in queue  # must stay False
            elif fin.get("action") == "already_routed":
                rec["observed"] = queue.get(fid)
        v.close()
        save(state)
        browser.close()

    rows = write_results(state)
    s = summary(rows)
    log("summary:", s)
    bad = [r["fax_id"] for r in rows if r["action"] in ("failed", "conflict", "outcome_unknown", None)]
    if bad:
        log("needs attention:", bad)
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(prog="linda_agent")
    ap.add_argument("--fresh", action="store_true", help="discard runs/ state and start over")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    a = ap.parse_args()
    sys.exit(run(a.fresh, a.headed))


if __name__ == "__main__":
    main()
