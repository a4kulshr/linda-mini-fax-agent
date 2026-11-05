# Engineering notes

## Architecture

```
session A (context 1)                 decide (pure)                      per fax: session B (fresh context + login)
login -> Fax Mgmt -> Inbound          rules.extract(page text)           read status -> fill form -> Review -> OK
walk all pages (status=All)    -->    rules.decide(all faxes)      -->   re-read detail + Routed Documents
download every fax, validate,         category, flags, completeness,     then: one more fresh session cross-checks
OCR every page                        duplicate groups, disposition      every claimed route
```

- `linda_agent/portal.py`: every UI interaction. It finds a fax by exact ID: search, click the link whose text is exactly the ID, then assert the ID on the detail page, the route form and the confirmation modal. Any page can bounce to the login form; it re-logs in and navigates back to the intended fax.
- `linda_agent/ocr.py`: PDF validation and per-page OCR. A download counts only if it starts with `%PDF-`, fully decodes, and has the page count the inbox shows.
- `linda_agent/rules.py`: extraction and decision. No browser, unit-tested.
- `linda_agent/__main__.py`: phases, checkpoints, retries, results.

**Why all downloads happen before any session B.** Duplicate handling ("route the earliest original") needs every fax's content before deciding anything, so session A reads the whole inbox first. Already-routed faxes are read too, so a resend of something routed before the run is caught. The portal accepts a route only if the fax was downloaded in a different, earlier session. Doing all downloads first and creating each B afterwards satisfies that for every fax. Each routed fax gets its own new browser context and login, so there is no ambiguity about what "a fresh session B" means.

## Decision rules

- **Category** comes from each page's document heading (the line under the scan banner), never from body text. That stops "imaging reports" mentioned inside a records request from counting as imaging. A cover page counts for nothing. A supporting page whose heading maps to the same category (the release authorization in FX-206) doesn't create a second category.
- **Needs Review** if any of these hold:
  - an explicit URGENT/CRITICAL/STAT flag (negated phrases like "No urgent flag" are ignored; the flagged line is quoted first in the note);
  - more than one category, or the packet says it holds multiple documents;
  - incomplete/missing/illegible wording;
  - an unreadable page;
  - "Page X of Y" disagreeing with the PDF;
  - a footer naming a different transmission;
  - no category.
- **Duplicates.** Faxes sharing a normalized source ID (a resend's "Original Document ID" counts) form a group. The earliest received one is the original.
  - A later copy is a confirmed resend only if category, patient, DOB and flag status don't conflict (a missing value is unknown, not a conflict). It becomes `skipped_duplicate` and is never submitted.
  - Same ID but conflicting details, a received-time tie, or a later copy that adds a flag: Needs Review, with the related fax IDs in the note.
  - An already-routed original makes its resends `skipped_duplicate`.
  - An already-routed resend whose original is new makes the original a `conflict`, reported and not submitted, so the source never gets a second route.

## State and idempotency

- `runs/state.json` is rewritten atomically (temp file + `os.replace`) after every step. Each fax moves through these steps: inventory, PDF hash + evidence, decision, `submitting`, final.
- **`submitting` is written before OK is clicked.** After any submit outcome (success page, 503, timeout, login page, crash) the worker never resubmits blind. It navigates fresh to the fax and reads the status.
  - Routed with our values: `verified`, or `recovered` if the response was not the success page or we're resuming.
  - Routed with other values: `conflict`.
  - Still New: retry, at most 3 attempts, each in a new session B.
- **Decisions are frozen once recorded.** On resume our own routes show as "Routed" and must not be re-decided as `already_routed`. The crash-after-submit test caught exactly this bug.
- **Resume.** Finished faxes are left alone; PDFs whose hash still matches are not re-downloaded. If the state says we routed a fax but the portal shows New (the sandbox was reset), the worker stops and asks for `--fresh` instead of guessing.
- Results are generated from state, so each fax appears once however many times the worker restarts.
- The portal's one-route-per-fax constraint is a backstop, not the mechanism. Nothing relies on it.

## Failure decisions

| Situation | Behaviour |
|---|---|
| Download returns an HTML 503 page | No download event arrives, so the attempt counts as failed and the alert text is recorded. The fax is reopened via the UI and retried (3 attempts with backoff). Bytes are only accepted after PDF validation. |
| Session expires (anywhere) | The login form is detected by URL, the worker re-logs in and navigates back to the same fax ID. On submit, the status is re-read before refilling the form (new CSRF token). |
| Route saved but response is 503 / timeout | The fax detail and Routed Documents are re-read. Matching values mean `recovered`, with no second submit. |
| Portal says "separate session required" (409) | The fax is re-downloaded in a new session A, and the next attempt uses a new B. |
| Process killed | Resume from `state.json`. Covered by tests for a kill before and after the submit click. |
| Retries exhausted | `failed` (never submitted) or `outcome_unknown` (a submit happened and the result couldn't be observed). The run exits 1. |

## Known gaps

- **The classifier is fitted to this document style.** It's heading keywords and labelled `X: VALUE` fields. Different layouts, handwriting or rotated scans would mostly land in Needs Review (safe, but more manual work) rather than be misrouted. A vision LLM producing structured output, still checked by these rules, is the natural upgrade.
- **Saved routing notes can't be verified.** The UI never shows them, so verification covers assignee, document type and folder only. Notes are capped at 1,000 characters (the portal truncates beyond that) and lead with the flag or reason.
- **One worker at a time.** There's no lock file; two concurrent workers on the same `runs/` could race (the portal's per-fax uniqueness still prevents a double route of the same fax).
- **Checkpoint durability.** Writes are atomic but not fsync'd, so a power loss (not a process kill) could lose the last step. The re-read-before-submit design limits the damage to a redundant status check.
- **A failed download hides that fax from duplicate grouping.** If a fax never downloads, it's reported `failed`, but other faxes are still decided without it, so a resend of it could be routed. This is unlikely given the retries, but it's not covered.
- **OCR determinism.** Thresholds were tuned on Tesseract 5.5, and other versions may shift confidence slightly.
