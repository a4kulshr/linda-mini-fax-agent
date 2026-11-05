"""OCR text -> evidence -> routing decision. Pure functions, no browser."""
import re

ROUTES = {
    "referral": ("Olivia Chen - Referrals", "Referral", "Referrals / New"),
    "lab": ("Noah Patel - Clinical Review", "Lab Result", "Clinical / Labs"),
    "imaging": ("Noah Patel - Clinical Review", "Imaging Report", "Clinical / Imaging"),
    "prior_auth": ("Ethan Brooks - Authorizations", "Prior Authorization", "Authorizations / Pending"),
    "refill": ("Ava Wilson - Nursing / Medication", "Refill Request", "Nursing / Medication"),
    "records": ("Mia Gomez - Medical Records", "Records Request", "Records / Requests"),
    "eob": ("Liam Rivera - Billing", "EOB", "Billing / EOBs"),
    "review": ("Jordan Ellis - Manual Review", "Needs Review", "Review / Unclassified"),
}

# Matched against a page's document title (its heading), most specific first.
# Body text is ignored for category so incidental mentions ("imaging reports" in a records request) don't count.
TITLE_RULES = [
    ("prior_auth", r"PRIOR AUTH"),
    ("records", r"MEDICAL RECORDS|RECORDS REQUEST|RELEASE (OF )?(INFORMATION|RECORDS)|RELEASE INFORMATION"),
    ("eob", r"EXPLANATION OF BENEFITS|\bEOB\b|REMITTANCE"),
    ("refill", r"REFILL|PRESCRIPTION|RX RENEWAL"),
    ("referral", r"REFERRAL|CONSULT"),
    ("imaging", r"IMAGING|RADIOLOGY|\bMRI\b|\bCT\b|ULTRASOUND|X-RAY"),
    ("lab", r"LABORATORY|LAB RESULT|LAB REPORT|PATHOLOGY"),
]
COVER = re.compile(r"\bCOVER\b")
BOILERPLATE = re.compile(r"FAX TRANSMISSION|SCANNED DOCUMENT|CONFIDENTIAL")

FLAG = re.compile(r"\b(URGENT|CRITICAL|STAT|EMERGENT|ASAP)\b", re.I)
NEGATED = re.compile(r"\b(no|not|non)\b[^.]*\b(urgent|critical|stat|emergent)\b", re.I)
INCOMPLETE = re.compile(r"\bincomplete\b|\bmissing\b|\billegible\b|\bunreadable\b|cannot be read", re.I)
MULTIPLE = re.compile(r"multiple (documents|enclosures)|different purposes|mixed packet", re.I)
RESEND = re.compile(r"\bRESEND\b|RETRANSMI|\bORIGINAL DOCUMENT ID\b|SAME .* AS PREVIOUSLY SENT", re.I)

ID_LINE = re.compile(r"^\s*(?P<label>(?:[A-Za-z][A-Za-z ]{0,40}?)?(?:ID|Id|reference|Reference|number|Number|Accession))\s*:\s*"
                     r"(?P<value>[A-Z0-9][A-Z0-9lIO]{0,5}-[A-Z0-9lIO]{2,10})\b", re.M)
NOT_SOURCE = re.compile(r"claim|member|callback|phone|transmission", re.I)
PATIENT = re.compile(r"^\s*(?:Patient|Member)\s*:\s*(.+)$", re.M)
DOB = re.compile(r"^\s*(?:DOB|Date of Birth)\s*:\s*([0-9/\-]+)", re.M)
PAGE_OF = re.compile(r"Page\s*([0-9lIO]{1,3})\s*of\s*([0-9lIO]{1,3})", re.I)
FOOTER_FAX = re.compile(r"Transmission\s+(FX-\d+)")


def _digits(s: str) -> int:
    return int(s.replace("l", "1").replace("I", "1").replace("O", "0").replace("o", "0"))


def norm_id(value: str) -> str:
    prefix, _, rest = value.upper().partition("-")
    return prefix + "-" + rest.replace("O", "0").replace("I", "1").replace("L", "1")


def page_title(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if "SCANNED DOCUMENT" in line and i + 1 < len(lines):
            return lines[i + 1]
    # No banner: first all-caps line that isn't boilerplate (skips letterhead only if it is first).
    caps = [l for l in lines if l.isupper() and len(l) > 6 and not BOILERPLATE.search(l)]
    return caps[1] if len(caps) > 1 else (caps[0] if caps else "")


def title_category(title: str):
    if COVER.search(title):
        return "cover"
    for cat, pat in TITLE_RULES:
        if re.search(pat, title, re.I):
            return cat
    return None


def extract(fax_id: str, pages: list[dict]) -> dict:
    """Evidence from every page. Each finding keeps (page, line) so results are auditable."""
    ev = {"pages": [], "flags": [], "incomplete": [], "multiple": [], "resend": [],
          "ids": [], "patient": None, "dob": None, "problems": []}
    seen_numbers = []
    for p in pages:
        n, text = p["page"], p["text"]
        title = page_title(text)
        cat = title_category(title)
        ev["pages"].append({"page": n, "title": title, "category": cat,
                            "readable": p["readable"], "confidence": p["confidence"]})
        if not p["readable"]:
            ev["problems"].append(f"page {n} unreadable (OCR confidence {p['confidence']})")
        for line in text.splitlines():
            line = line.strip()
            if FLAG.search(line) and not NEGATED.search(line):
                ev["flags"].append({"page": n, "line": line})
            if INCOMPLETE.search(line):
                ev["incomplete"].append({"page": n, "line": line})
            if MULTIPLE.search(line):
                ev["multiple"].append({"page": n, "line": line})
            if RESEND.search(line):
                ev["resend"].append({"page": n, "line": line})
        for m in ID_LINE.finditer(text):
            if not NOT_SOURCE.search(m["label"]):
                ev["ids"].append({"page": n, "label": m["label"].strip(), "value": norm_id(m["value"])})
        if ev["patient"] is None and (m := PATIENT.search(text)):
            ev["patient"] = m[1].strip()
        if ev["dob"] is None and (m := DOB.search(text)):
            ev["dob"] = m[1].strip()
        if (m := PAGE_OF.search(text)):
            seen_numbers.append((n, _digits(m[1]), _digits(m[2])))
        for other in set(FOOTER_FAX.findall(text)) - {fax_id}:
            ev["problems"].append(f"page {n} footer names transmission {other}, not {fax_id}")

    total = len(pages)
    for n, x, y in seen_numbers:
        if y != total:
            ev["problems"].append(f"page {n} says 'of {y}' but the PDF has {total} pages")
        elif x != n:
            ev["problems"].append(f"page {n} is numbered {x} (missing or out-of-order page)")

    cats = {p["category"] for p in ev["pages"]} - {None, "cover"}
    ev["categories"] = sorted(cats)
    # The primary document's own ID; a resend points at the original's ID.
    original = [i for i in ev["ids"] if "original" in i["label"].lower()]
    doc_ids = original or [i for i in ev["ids"] if i["page"] in
                           [p["page"] for p in ev["pages"] if p["category"] not in (None, "cover")]] or ev["ids"]
    ev["source_id"] = doc_ids[0]["value"] if doc_ids else None
    return ev


def content_verdict(ev: dict) -> tuple:
    """(category, reasons). Any reason means Needs Review."""
    reasons = []
    if ev["flags"]:
        f = ev["flags"][0]
        reasons.append(f"explicit flag on p{f['page']}: \"{f['line']}\"")
    if len(ev["categories"]) > 1:
        reasons.append("mixed packet: " + ", ".join(ev["categories"]))
    if ev["multiple"]:
        reasons.append(f"packet declares multiple documents (p{ev['multiple'][0]['page']})")
    if ev["incomplete"]:
        i = ev["incomplete"][0]
        reasons.append(f"incomplete/unclear on p{i['page']}: \"{i['line']}\"")
    reasons += ev["problems"]
    if not ev["categories"]:
        reasons.append("no document category established from page headings")
    cat = ev["categories"][0] if len(ev["categories"]) == 1 else None
    return cat, reasons


def _same_context(a: dict, b: dict) -> list:
    """Conflicts between two copies; missing values are unknown, not conflicts."""
    out = []
    for k in ("patient", "dob"):
        if a[k] and b[k] and a[k].lower() != b[k].lower():
            out.append(f"{k} differs ({a[k]!r} vs {b[k]!r})")
    if a["categories"] != b["categories"]:
        out.append(f"category differs ({a['categories']} vs {b['categories']})")
    if bool(a["flags"]) != bool(b["flags"]):
        out.append("only one copy carries an urgent/critical flag")
    return out


def decide(faxes: list[dict]) -> dict:
    """faxes: [{fax_id, received_at, status, routed (saved fields or None), evidence}] -> {fax_id: decision}."""
    out = {}
    for f in faxes:
        cat, reasons = content_verdict(f["evidence"])
        out[f["fax_id"]] = {"category": cat, "reasons": reasons, "duplicate_of": None, "related": []}

    groups = {}
    for f in faxes:
        if f["evidence"]["source_id"]:
            groups.setdefault(f["evidence"]["source_id"], []).append(f)

    for sid, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda f: (f["received_at"], f["fax_id"]))
        first = members[0]
        ids = [m["fax_id"] for m in members]
        tie = members[1]["received_at"] == first["received_at"]
        for m in members[1:]:
            d = out[m["fax_id"]]
            d["related"] = [i for i in ids if i != m["fax_id"]]
            conflicts = _same_context(first["evidence"], m["evidence"])
            if tie:
                conflicts.append(f"received at the same time as {first['fax_id']}; original cannot be determined")
            if conflicts:
                d["reasons"].append(f"possible resend of {first['fax_id']} (source {sid}) but " + "; ".join(conflicts))
            else:
                d["duplicate_of"] = first["fax_id"]
        if tie:
            out[first["fax_id"]]["reasons"].append(f"shares source {sid} with {ids[1:]} at the same received time")
            out[first["fax_id"]]["related"] = ids[1:]

    # Disposition.
    for f in faxes:
        d = out[f["fax_id"]]
        if f["status"] == "Routed":
            d["action"] = "already_routed"
        elif d["duplicate_of"]:
            orig = next(x for x in faxes if x["fax_id"] == d["duplicate_of"])
            d["action"] = "skipped_duplicate"
            d["note"] = (f"confirmed resend of {orig['fax_id']} (source {f['evidence']['source_id']}); "
                         f"original is {'already routed' if orig['status'] == 'Routed' else 'routed by this run'}")
        else:
            d["action"] = "needs_review" if d["reasons"] else "route"
        if d["action"] in ("route", "needs_review"):
            # A later copy already routed while this earlier one is new: don't create a second route.
            later_routed = [x["fax_id"] for x in faxes if x["fax_id"] != f["fax_id"] and x["status"] == "Routed"
                            and out[x["fax_id"]].get("duplicate_of") == f["fax_id"]]
            if later_routed:
                d["action"] = "conflict"
                d["reasons"].append(f"resend {later_routed} is already routed; not creating a second route")
                continue
            key = "review" if d["action"] == "needs_review" else d["category"]
            d["fields"] = dict(zip(("assignee", "doc_type", "folder"), ROUTES[key]))
            d["note"] = ("; ".join(d["reasons"]) if d["reasons"] else
                         f"{d['category']} per page headings; source {f['evidence']['source_id']}")[:1000]
    return out
