# Linda Mini fax routing agent: design

Take-home for TriFetch. Deadline 2026-11-05 12:00 PDT. Spec: `~/Documents/TriFetch_Linda_Browser_Agent_Take_Home.pdf`; sandbox README and `app.py` in this folder.

## Intent

A worker that drives the Linda Mini portal through a real browser, reads every scanned fax with OCR, and routes each one per the SOP, without ever creating a wrong or duplicate route. Graders stress test reliability (fault modes, restarts) and judge: end-to-end browser execution, complete-document extraction, exact routing, safe exceptions, separate authenticated sessions, idempotency, reproducibility, honest limits.

Guiding rule: when unsure, route to Needs Review (or stop and report), never guess. A visible "failed/unknown" beats a silent wrong route.

## Goals / non-goals / anti-goals

- Goals: full inbox processed; correct SOP routing; all four fault classes recovered; resume after kill with no double submit; per-fax results file; reproducible with one command.
- Non-goals: LLM/vision model (named as upgrade path only); generality beyond this portal's UI; concurrency/multiple workers; production durability (fsync, locks).
- Anti-goals (failure even if it "works"): any portal access outside the browser UI (no DB, no `__admin`, no reading `data/faxes`); a second route for the same source document; trusting a click or HTTP status as confirmation; clinical interpretation (we only detect explicit flags).

## Stack (decided)

Python 3.11+, Playwright (sync API, Chromium), PyMuPDF to render pages (300 dpi), pytesseract + Tesseract 5 for OCR. Fully offline, deterministic. Code lives in this folder as a `linda_agent/` package (plus `tests/`, `demo.sh`), dependencies in `requirements-agent.txt`. Credentials via env vars with the training defaults; base URL configurable.

## Architecture

Four phases, all state in `runs/state.json` (tmp-file + rename on every transition). `results.json` and `results.csv` are generated from state.

1. **Inventory (session A).** Fresh browser context, log in via the login form, click Fax Management -> Inbound Faxes. Clear search, status All, walk pages with the visible Next link until there are no more pages. Record fax ID, received, sender, pages, status for each row. Check the unique count equals the "N transmissions" total shown. Inventory is complete before any mutation.

2. **Download + extract (session A).** For every fax (including already-routed ones, which are read but never submitted): click the exact fax-ID link in the table, assert the detail page shows that ID, click Download Fax PDF.
   - Outcomes handled separately: download event -> save to temp, validate (parses as PDF, page count equals the UI page count, every page renders), then rename to `runs/downloads/FX-xxx.pdf` and store sha256; page navigation instead (503 HTML page) -> failed attempt; login page -> re-login, navigate back to the fax, retry. Max 3 attempts with backoff, then the fax is `failed` with the error recorded.
   - OCR each page. Store per-page text, mean confidence, and whether the page was readable (enough characters/confidence).
   - Extraction (rules over OCR text, per page): category evidence by headings/title and requested action (not incidental mentions), explicit flags `URGENT`/`CRITICAL`/`STAT`, source document ID by labelled patterns ("Document ID", "Ref", "Order #" etc., normalized for O/0 and I/1), patient name and DOB when labelled, "Page X of Y" markers. Evidence = matched line + page number.

3. **Decide (pure function, no browser).** Input: all extractions plus inventory. Order:
   1. **Per-fax content verdict:** a single category with clear evidence, or review with a reason. Review reasons: explicit urgent/critical flag (quoted), more than one distinct category, an unreadable page, page-number gaps/duplicates or a "of Y" total disagreeing with the page count, no category found.
   2. **Duplicate groups:** faxes sharing a normalized source document ID **and** agreeing on available context (patient name/DOB, category). A shared ID with conflicting context = identity uncertain -> every member goes to review with that reason. Missing context counts as unknown, not a match. Sender/time is never identity.
   3. **Canonical = earliest received in the group.** A tie on received time -> the group goes to review. A later copy that adds a flag, extra pages, or a different category is not a duplicate -> it gets its own verdict.
   4. **Disposition:** canonical already routed -> `already_routed` (left unchanged), its resends `skipped_duplicate`. Canonical new -> route per its verdict, resends `skipped_duplicate` with `duplicate_of`. A resend already routed while the canonical is new -> do not route the canonical; mark the group for review with a `conflict` note in the results (no submit). A non-duplicate already routed -> `already_routed`.
   - Routing note: for review, the flag/reason first ("URGENT flag on p1: '...'"), capped at 1000 chars.
   - The decision records the PDF sha256 it was based on.

4. **Route (fresh session B per fax).** Only after phases 1-3 finish for every fax. For each fax with a submit decision: new browser context, log in, navigate via the UI (Fax Management -> Inbound Faxes -> search the ID -> click the exact ID link), assert ID. Read status:
   - Routed -> compare the saved fields with the decision; match = `verified` (recovered if we had a submit attempt), mismatch = `conflict`. Never submit.
   - New -> Route / Assign Fax, `select_option(label=...)` for the three dropdowns, fill the note, assert the selections, click Review Routing, wait for the visible confirmation dialog (DOM modal, not a JS dialog), assert its fax ID and three summaries. **Write `submitting` (attempt id, fields) to state**, then click OK - Submit Routing.
   - After the click, whatever the response (success, 503, timeout, login page): re-login if needed, navigate fresh to the fax detail, read status and saved fields, then also check that the fax appears in Routed Documents with the same values. Match -> `verified`. Still New after a seen failure -> retry (max 3). Routed with different values -> `conflict`. Can't observe -> `outcome_unknown`.
   - 409 "separate session required" -> the download is not older than this session; re-download in a new session A, then a new session B.

## Restart / idempotency

- State per fax: `listed -> downloaded -> extracted -> decided -> submitting -> final(action, verification)`.
- On start: if state exists, resume from it; inventory re-runs and is reconciled (a new fax is added, a status change is noted). Any fax in `submitting` or not final is re-observed in the UI before anything else (as above). Final faxes are not redone. Results are regenerated from state, so each fax is reported once.
- Re-downloads after restart happen before any session B, which preserves the portal's download-before-session rule.
- `--fresh` flag discards state (used by the demo after `reset_sandbox.py`).
- Portal uniqueness (one route per fax ID) is a backstop, not our idempotency mechanism.

## Results

`results.json` (and CSV) per fax: fax_id, received, sender, pdf_sha256, pages (ui/pdf/readable), extracted category + evidence (page, line), source_id, patient/DOB if seen, flags, review_reasons, duplicate_of / group, action (`routed` | `needs_review` | `skipped_duplicate` | `already_routed` | `failed` | `conflict` | `outcome_unknown`), selected fields + note, observed saved fields, verification (`verified` | `recovered` | `not_applicable` | `unverified`) with "notes not visible in UI" stated, attempts + errors per phase. Plus a run summary.

## Testing

- `pytest` unit tests on decide() and extraction using OCR text captured from real runs (normal, urgent, mixed, incomplete, duplicate, tie, conflicting identity, already-routed original).
- `demo.sh`: for each mode (`none`, `download_503_once`, `session_expire_on_route_once`, `route_commit_then_503_once`): stop server, reset, start the server with the mode, run the worker `--fresh`, then assert from `results.json` and the UI (browser) that the expected outcomes are verified, that two sessions were used, and that a second run makes no new submissions.
- A kill test: an env hook (`LINDA_AGENT_CRASH_AT=before_submit|after_submit`) makes the worker exit at that point; rerun and assert no duplicate and a verified result.

## Deliverables

README (setup incl. `brew install tesseract` + `playwright install chromium`, one run command, config, credentials, OCR tooling), NOTES.md (architecture, idempotency, failure decisions, known gaps), sample `results.json`, tests, demo script.

## Decision clarifications (builder check; my calls)

- "Goes to review" always means submitting the Needs Review route (Jordan Ellis / Needs Review / Review / Unclassified) with the reason in the note. Only the "resend already routed, canonical new" conflict is report-only.
- Confirmed duplicate = same normalized source ID + same category + no conflicting context (name, DOB, flags, page count). Missing context doesn't block; conflicting context does. Confirmed resends are `skipped_duplicate`, never routed.
- Same source ID but not confirmed (tie on received time, conflicting context, later copy adds a flag/pages): each such fax goes to Needs Review with a note naming the related fax IDs. Spec: "If not certain, request review." Two review routes for a possible duplicate are acceptable because a human resolves them; a silent skip or a second normal route is not.
- Classification is per packet: the primary document's requested action decides the category. Pages that belong to it (cover sheet, attached results it references) don't count as a second category. "Mixed" = a second independent document with its own header/identifier.
- Rules stay generic (headings and labelled fields), not fitted to these 12 files, since graders may swap content.

## Left to the builder

Module layout, exact regexes and OCR thresholds (tune on real OCR output from the portal downloads), retry backoff values, log format.

## Known gaps (to state honestly)

Keyword/heading classifier won't generalize to unseen layouts (LLM vision is the upgrade path); no multi-process lock; saved notes can't be verified through the UI; no fsync-level durability; Tesseract output can vary slightly across versions.
