"""Decision rules on OCR text captured from the portal downloads, plus synthetic edge cases."""
import copy
import json
from pathlib import Path

from linda_agent.rules import decide, extract

FIX = json.loads((Path(__file__).parent / "fixtures" / "sandbox_ocr.json").read_text())

EXPECTED = {
    "FX-201": ("route", "Referral"), "FX-202": ("needs_review", "Needs Review"),
    "FX-203": ("route", "Imaging Report"), "FX-204": ("route", "Prior Authorization"),
    "FX-205": ("route", "Refill Request"), "FX-206": ("route", "Records Request"),
    "FX-207": ("route", "EOB"), "FX-208": ("route", "Lab Result"),
    "FX-209": ("needs_review", "Needs Review"), "FX-210": ("skipped_duplicate", None),
    "FX-211": ("already_routed", None), "FX-212": ("needs_review", "Needs Review"),
}


def faxes(ocr=None, inv=None):
    ocr = ocr or FIX["ocr"]
    return [dict(f, evidence=extract(f["fax_id"], ocr[f["fax_id"]])) for f in (inv or FIX["inventory"])]


def page(text, n=1, readable=True):
    return {"page": n, "text": text, "confidence": 93.0 if readable else 20.0, "readable": readable}


def test_sandbox_inbox():
    out = decide(faxes())
    got = {k: (v["action"], v.get("fields", {}).get("doc_type")) for k, v in out.items()}
    assert got == EXPECTED
    assert out["FX-210"]["duplicate_of"] == "FX-201"
    assert "CRITICAL" in out["FX-202"]["note"]                      # flag quoted in the note
    assert out["FX-202"]["fields"]["assignee"] == "Jordan Ellis - Manual Review"


def test_negated_flag_is_not_urgent():
    ev = extract("FX-208", FIX["ocr"]["FX-208"])                    # "No urgent flag provided on this report."
    assert ev["flags"] == []


def test_supporting_page_is_not_a_second_category():
    ev = extract("FX-206", FIX["ocr"]["FX-206"])                    # records request + authorization to release
    assert ev["categories"] == ["records"]


def test_page_gap_unreadable_page_and_wrong_footer_go_to_review():
    body = "ACME LABS\nFAX TRANSMISSION / SCANNED DOCUMENT\nLABORATORY REPORT\nReport ID: LAB-1\n"
    ev = extract("FX-1", [page(body + "Transmission FX-1 Page 1 of 3"), page(body + "Transmission FX-9 Page 3 of 3", 2),
                          page("", 3, readable=False)])
    reasons = " ".join(decide([{"fax_id": "FX-1", "received_at": "1", "status": "New", "evidence": ev}])["FX-1"]["reasons"])
    assert "page 2 is numbered 3" in reasons
    assert "page 3 unreadable" in reasons and "FX-9" in reasons


def _with_copy(fid, received, text_edit=lambda t: t, status="New"):
    ocr = copy.deepcopy(FIX["ocr"])
    ocr[fid] = [dict(p, text=text_edit(p["text"]).replace("FX-201", fid)) for p in ocr["FX-201"]]
    inv = copy.deepcopy(FIX["inventory"]) + [{"fax_id": fid, "received_at": received, "sender": "x", "ui_pages": 2, "status": status}]
    return decide(faxes(ocr, inv))


def test_tie_on_received_time_goes_to_review_for_both():
    out = _with_copy("FX-299", "2026-11-03 08:02")
    assert out["FX-201"]["action"] == out["FX-299"]["action"] == "needs_review"


def test_later_copy_with_new_urgent_flag_is_not_silently_skipped():
    out = _with_copy("FX-299", "2026-11-03 10:00", lambda t: t.replace("Priority: Routine", "URGENT - same day"))
    assert out["FX-299"]["action"] == "needs_review" and "FX-201" in out["FX-299"]["reasons"][-1]
    assert out["FX-201"]["action"] == "route"


def test_conflicting_patient_blocks_duplicate():
    out = _with_copy("FX-299", "2026-11-03 10:00", lambda t: t.replace("Elena Hart", "Elena Hartley"))
    assert out["FX-299"]["action"] == "needs_review" and out["FX-299"]["duplicate_of"] is None


def test_resend_of_already_routed_original_is_skipped():
    inv = copy.deepcopy(FIX["inventory"])
    next(f for f in inv if f["fax_id"] == "FX-201")["status"] = "Routed"
    out = decide(faxes(inv=inv))
    assert out["FX-201"]["action"] == "already_routed" and out["FX-210"]["action"] == "skipped_duplicate"


def test_routed_resend_blocks_routing_the_original():
    inv = copy.deepcopy(FIX["inventory"])
    next(f for f in inv if f["fax_id"] == "FX-210")["status"] = "Routed"
    out = decide(faxes(inv=inv))
    assert out["FX-201"]["action"] == "conflict" and "fields" not in out["FX-201"]
