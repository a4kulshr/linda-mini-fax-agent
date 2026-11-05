# Linda Mini fax routing agent

A worker that drives the Linda Mini portal through a real browser (Playwright/Chromium). It downloads every inbound fax, OCRs every page with Tesseract, applies the routing SOP, and routes each eligible fax from a separate, freshly authenticated session. Each result is confirmed by re-reading the portal UI. The portal itself is unchanged; its original README is [SANDBOX_README.md](SANDBOX_README.md). Design, state and failure handling are in [NOTES.md](NOTES.md).

## Setup

Requires Python 3.11+ and Tesseract 5 (`brew install tesseract` on macOS, `apt install tesseract-ocr` on Debian/Ubuntu).

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-agent.txt     # portal + agent deps
.venv/bin/playwright install chromium
```

## Run

One command: reset the sandbox, start the portal (optionally with a fault mode), and run the worker:

```bash
./demo.sh                                  # normal
./demo.sh download_503_once                # or session_expire_on_route_once, route_commit_then_503_once
./demo.sh none --headed                    # watch the browser
```

Against a portal you started yourself:

```bash
.venv/bin/python -m linda_agent            # resumes from runs/state.json if present
.venv/bin/python -m linda_agent --fresh    # discard previous state (use after reset_sandbox.py)
```

Exit code is 0 when every fax reached a safe final state, and 1 if any fax is `failed`, `conflict` or `outcome_unknown`.

Output goes to `runs/`:

- `results.json`: per fax, the OCR page titles and confidence, source ID and every labelled identifier, flags with page and line, review reasons, duplicate link, action, selected fields, note, fields read back from the UI, verification, submit attempts, sessions used, and errors and retries.
- `results.csv`: the flat version.
- `state.json`: the checkpoint.
- `downloads/`: the PDFs as downloaded through the portal.

A sample from a clean run is in [sample_results/](sample_results/).

### Configuration (env vars)

| Variable | Default | |
|---|---|---|
| `LINDA_URL` | `http://127.0.0.1:8000` | portal base URL |
| `LINDA_USER` / `LINDA_PASSWORD` | `agent.demo` / `demo-password-123` | training credentials; only ever typed into the login form |
| `LINDA_RUNS` | `runs` | state and output directory |
| `LINDA_AGENT_CRASH_AT` | unset | test hook: `before_submit` / `after_submit` makes the worker exit abruptly there |

## Results on the supplied inbox

| Fax | Action | Why |
|---|---|---|
| FX-201 | routed: Referral | cover sheet + new patient referral RF-3001 |
| FX-202 | needs_review | explicit "CRITICAL RESULT" flag, quoted in the note |
| FX-203 | routed: Imaging Report | |
| FX-204 | routed: Prior Authorization | same received time as FX-203, different sender; matched by exact fax ID |
| FX-205 | routed: Refill Request | |
| FX-206 | routed: Records Request | page 2 is the release authorization for the same request (MR-4006), not a second category |
| FX-207 | routed: EOB | |
| FX-208 | routed: Lab Result | "No urgent flag provided" is correctly not treated as a flag |
| FX-209 | needs_review | no category, states it is incomplete |
| FX-210 | skipped_duplicate | resend of RF-3001 with the same patient and DOB; original FX-201 received earlier |
| FX-211 | already_routed | routed before the run; left untouched (its PDF is still OCR'd for duplicate detection) |
| FX-212 | needs_review | mixed packet: lab + imaging |

## Tests / replay

```bash
.venv/bin/python -m pytest -q          # ~4 min; SKIP_E2E=1 for the unit tests only
```

- `tests/test_rules.py`: decision rules on the OCR text captured from the portal downloads (the full expected inbox), plus edge cases: negated flags, supporting pages, page gaps, unreadable pages, a footer naming another fax, receive-time ties, a later copy that adds an urgent flag, conflicting patient details, a resend of an already-routed original, and an already-routed resend blocking its original.
- `tests/test_e2e.py`: each scenario starts its own portal (temp DB, free port) and runs the real worker in a subprocess. It covers:
  - a normal run, then a rerun that submits nothing;
  - `download_503_once` (the error page is not parsed as a PDF; retried);
  - `session_expire_on_route_once` (re-login, re-check, resubmit once);
  - `route_commit_then_503_once` (the UI shows it saved, so no resubmit; marked `recovered`);
  - a kill just before and just after the submit click, then a resume.

  Every scenario asserts the exact routing of all 12 faxes, that the saved fields were read back from the UI, and that routing ran in `B:` sessions.

## OCR / tooling choices

- **Tesseract 5 via pytesseract**, pages rendered at 300 dpi with PyMuPDF. It's local, free and deterministic, and the reviewer needs no API keys. Each page records mean word confidence, and a page below 40 characters or 55% confidence counts as unreadable, which forces review.
- **No LLM.** Classification is rules over each page's heading plus labelled fields. It's deterministic and every decision cites the page and line it came from. The tradeoff (it won't generalize to unseen layouts) is covered in NOTES.md.
- **Playwright (sync, Chromium)** for every portal action. No `__admin` endpoints, no SQLite access, no reading `data/faxes`.
