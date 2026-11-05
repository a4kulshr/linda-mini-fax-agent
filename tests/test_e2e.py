"""End-to-end replays: a real portal (own temp DB, own port) per scenario, the real worker in a subprocess.
Assertions come from the worker's results file, which it builds from what the browser UI showed."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_rules import EXPECTED

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(os.getenv("SKIP_E2E") == "1", reason="SKIP_E2E=1")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def portal(tmp_path):
    procs = []

    def start(fault="none"):
        port = free_port()
        env = dict(os.environ, LINDA_DB_PATH=str(tmp_path / "portal.sqlite3"), LINDA_FAULT_MODE=fault)
        p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
                             cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        return f"http://127.0.0.1:{port}"

    yield start
    for p in procs:
        p.terminate()
        p.wait()


def run_agent(url, runs, *args, crash=None):
    env = dict(os.environ, LINDA_URL=url, LINDA_RUNS=str(runs), PYTHONWARNINGS="ignore")
    env.pop("LINDA_AGENT_CRASH_AT", None)
    if crash:
        env["LINDA_AGENT_CRASH_AT"] = crash
    p = subprocess.run([sys.executable, "-m", "linda_agent", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    return p.returncode, p.stderr


def results(runs):
    return {r["fax_id"]: r for r in json.loads((runs / "results.json").read_text())["faxes"]}


def assert_inbox_correct(res):
    for fid, (action, doc_type) in EXPECTED.items():
        r = res[fid]
        want = {"route": "routed"}.get(action, action)
        assert r["action"] == want, (fid, r["action"], r["errors"])
        if doc_type:
            assert r["selected_fields"]["doc_type"] == doc_type
            assert r["observed_fields"] == r["selected_fields"], fid      # read back from the UI
            assert r["verification"] in ("verified", "recovered")
            assert r["sessions"] and all(s["name"].startswith("B:") for s in r["sessions"])


def test_normal_run_then_rerun_is_a_noop(portal, tmp_path):
    url, runs = portal(), tmp_path / "runs"
    code, log = run_agent(url, runs, "--fresh")
    assert code == 0, log
    first = results(runs)
    assert_inbox_correct(first)
    code, log = run_agent(url, runs)
    assert code == 0, log
    assert "submit attempt" not in log                                  # nothing resubmitted
    assert {k: v["submit_attempts"] for k, v in results(runs).items()} == {k: v["submit_attempts"] for k, v in first.items()}


def test_download_503_is_retried_not_parsed(portal, tmp_path):
    runs = tmp_path / "runs"
    code, log = run_agent(portal("download_503_once"), runs, "--fresh")
    assert code == 0, log
    res = results(runs)
    assert_inbox_correct(res)
    errs = [e for r in res.values() for e in r["errors"] if e["phase"] == "download"]
    assert len(errs) == 1 and "503" in errs[0]["error"]


def test_session_expiry_on_submit_relogs_and_verifies(portal, tmp_path):
    runs = tmp_path / "runs"
    code, log = run_agent(portal("session_expire_on_route_once"), runs, "--fresh")
    assert code == 0, log
    res = results(runs)
    assert_inbox_correct(res)
    r = res["FX-201"]
    assert r["submit_attempts"][0]["page_after_submit"] == "login_page" and len(r["submit_attempts"]) == 2


def test_commit_then_503_is_recovered_without_resubmit(portal, tmp_path):
    runs = tmp_path / "runs"
    code, log = run_agent(portal("route_commit_then_503_once"), runs, "--fresh")
    assert code == 0, log
    res = results(runs)
    assert_inbox_correct(res)
    r = res["FX-201"]
    assert len(r["submit_attempts"]) == 1 and "503" in r["submit_attempts"][0]["page_after_submit"]
    assert r["verification"] == "recovered"


@pytest.mark.parametrize("point", ["before_submit", "after_submit"])
def test_crash_and_resume(portal, tmp_path, point):
    url, runs = portal(), tmp_path / "runs"
    code, _ = run_agent(url, runs, "--fresh", crash=point)
    assert code == 70
    code, log = run_agent(url, runs)
    assert code == 0, log
    res = results(runs)
    assert_inbox_correct(res)
    r = res["FX-201"]
    if point == "after_submit":
        assert len(r["submit_attempts"]) == 1 and r["verification"] == "recovered"   # committed before the crash; not resubmitted
