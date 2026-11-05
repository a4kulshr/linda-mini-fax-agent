"""Render runs/results.json as a static HTML report: python -m linda_agent.report [results.json] [out.html]"""
import json
import sys
from html import escape
from pathlib import Path

LABELS = {"routed": "Routed", "needs_review": "Needs review", "skipped_duplicate": "Skipped duplicate",
          "already_routed": "Already routed", "conflict": "Conflict", "failed": "Failed",
          "outcome_unknown": "Outcome unknown"}
TONE = {"routed": "ok", "needs_review": "warn", "skipped_duplicate": "muted", "already_routed": "muted"}

CSS = """
:root{--bg:#FCFBF8;--card:#fff;--ink:#35423C;--muted:#6B7770;--line:#E9EFEA;--soft:#EDF2EE;
--green:#45D48E;--mint:#B7F5D9;--blue:#1B38C9;--warn:#B45309;--warnbg:#FEF3C7;--bad:#B91C1C;--badbg:#FEE2E2;
--display:'Bricolage Grotesque',system-ui,sans-serif;--body:'Instrument Sans',system-ui,sans-serif;--mono:'IBM Plex Mono',ui-monospace,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--body)}
.wrap{max-width:1120px;margin:0 auto;padding:40px 20px 80px}
.eyebrow{font:500 12px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
h1{font:700 40px/1.1 var(--display);margin:8px 0 6px;letter-spacing:-.02em}
.sub{color:var(--muted);margin:0 0 28px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:28px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.tile b{display:block;font:700 34px var(--display)}.tile span{color:var(--muted);font-size:13px}
.tile.ok{background:var(--mint);border-color:var(--mint)}
.fax{background:var(--card);border:1px solid var(--line);border-radius:14px;margin-bottom:10px}
summary{list-style:none;cursor:pointer;display:grid;grid-template-columns:90px 150px 1fr auto;gap:14px;align-items:center;padding:14px 18px}
summary::-webkit-details-marker{display:none}
.id{font:600 14px var(--mono)}.why{color:var(--muted);font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{display:inline-block;font:600 12px var(--mono);padding:3px 10px;border-radius:999px;background:var(--soft);color:var(--ink);white-space:nowrap}
.pill.ok{background:var(--mint);color:#14532D}.pill.warn{background:var(--warnbg);color:var(--warn)}
.pill.bad{background:var(--badbg);color:var(--bad)}.pill.muted{background:var(--soft);color:var(--muted)}
.check{font:500 12px var(--mono);color:var(--muted)}.check.v{color:#15803D}
.body{border-top:1px solid var(--line);padding:16px 18px;display:grid;grid-template-columns:1fr 1fr;gap:10px 28px}
.body h4{margin:0 0 4px;font:600 12px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.body p,.body ul{margin:0 0 6px}.body ul{padding-left:18px}.mono{font:13px var(--mono)}
.full{grid-column:1/-1}
footer{margin-top:36px;color:var(--muted);font-size:13px}
@media(max-width:720px){summary{grid-template-columns:1fr auto}.why{display:none}.body{grid-template-columns:1fr}h1{font-size:30px}}
"""


def li(items):
    return "<ul>" + "".join(f"<li>{escape(str(i))}</li>" for i in items) + "</ul>" if items else "<p>-</p>"


def fax_card(r):
    a = r["action"] or "pending"
    f = r["selected_fields"] or {}
    reason = (r["review_reasons"] or [None])[0] or (f"resend of {r['duplicate_of']}" if r["duplicate_of"] else "") \
        or f.get("doc_type") or ("routed before this run; left untouched" if a == "already_routed" else "")
    ver = (r["verification"] or "").replace("_", " ")
    obs = r["observed_fields"] or {}
    attempts = [f"#{x['attempt']} in {x['session']}: {x['page_after_submit']}" for x in r["submit_attempts"] or []]
    ocr = ", ".join(f"p{p['page']} {p['confidence']:.0f}%" for p in r["ocr_pages"] or [])
    return f"""<details class="fax"><summary>
<span class="id">{escape(r['fax_id'])}</span><span><span class="pill {TONE.get(a, 'bad')}">{escape(LABELS.get(a, a))}</span></span>
<span class="why">{escape(reason)}</span><span class="check {'v' if ver in ('verified', 'recovered') else ''}">{escape(ver)}</span></summary>
<div class="body">
<div><h4>Received</h4><p>{escape(str(r['received_at']))} &middot; {escape(str(r['sender']))} &middot; {r['pages_ui']} page(s)</p></div>
<div><h4>Pages (OCR confidence)</h4>{li(f'p{p}: {t}' for p, t in r['page_titles'] or [])}<p class="mono">{escape(ocr)}</p></div>
<div><h4>Routed to</h4><p>{escape(' / '.join(filter(None, [f.get('assignee'), f.get('doc_type'), f.get('folder')])) or '-')}</p></div>
<div><h4>Read back from portal</h4><p>{escape(' / '.join(filter(None, obs.values())) or '-')}</p></div>
<div><h4>Source ID</h4><p class="mono">{escape(str(r['source_id'] or '-'))}</p></div>
<div><h4>Flags</h4>{li(f"p{x['page']}: {x['line']}" for x in r['flags'] or [])}</div>
<div class="full"><h4>Review reasons</h4>{li(r['review_reasons'])}</div>
<div class="full"><h4>Submit attempts</h4>{li(attempts)}</div>
<div class="full"><h4>Errors and retries</h4>{li(e['error'] for e in r['errors'] or [])}</div>
</div></details>"""


def render(data):
    s, faxes = data["summary"], data["faxes"]
    tiles = "".join(f'<div class="tile {"ok" if k == "routed" else ""}"><b>{v}</b><span>{escape(LABELS.get(k, k))}</span></div>'
                    for k, v in s.items())
    checked = sum(1 for r in faxes if r["verification"] in ("verified", "recovered"))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Linda Mini run report</title>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700&family=Instrument+Sans:wght@400;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body><div class="wrap">
<div class="eyebrow">Linda Mini &middot; fax routing run</div>
<h1>{len(faxes)} faxes processed</h1>
<p class="sub">{checked} submitted routes confirmed by re-reading the portal UI. Click a fax for the evidence behind its decision.</p>
<div class="tiles">{tiles}</div>
{''.join(fax_card(r) for r in faxes)}
<footer>Generated from results.json. Routing notes are not shown by the portal, so verification covers assignee, document type and folder.</footer>
</div></body></html>"""


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "runs/results.json")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else src.with_name("report.html"))
    out.write_text(render(json.loads(src.read_text())))
    print(out)


if __name__ == "__main__":
    main()
