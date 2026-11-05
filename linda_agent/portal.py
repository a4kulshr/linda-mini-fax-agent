"""Every portal interaction goes through the visible browser UI."""
import os
import re
import time
from pathlib import Path

from playwright.sync_api import Browser, Page, TimeoutError as PWTimeout

BASE_URL = os.getenv("LINDA_URL", "http://127.0.0.1:8000")
USERNAME = os.getenv("LINDA_USER", "agent.demo")
PASSWORD = os.getenv("LINDA_PASSWORD", "demo-password-123")


class PortalError(Exception):
    pass


class Session:
    """One isolated browser context with its own login."""

    def __init__(self, browser: Browser, name: str):
        self.name = name
        self.ctx = browser.new_context(accept_downloads=True)
        self.page: Page = self.ctx.new_page()
        self.page.set_default_timeout(15_000)
        self.logins = 0
        self.page.goto(BASE_URL + "/login")
        self.login()

    def close(self):
        self.ctx.close()

    def on_login_page(self) -> bool:
        return "/login" in self.page.url

    def login(self):
        p = self.page
        p.fill("#username", USERNAME)
        p.fill("#password", PASSWORD)
        p.get_by_role("button", name="Sign in").click()
        p.wait_for_load_state()
        if self.on_login_page():
            raise PortalError("login rejected")
        self.logins += 1

    def relogin_if_needed(self) -> bool:
        if self.on_login_page():
            self.login()
            return True
        return False

    def _click_nav(self, name: str):
        self.page.locator("aside").get_by_role("link", name=name, exact=True).click()
        self.page.wait_for_load_state()
        self.relogin_if_needed()

    def open_inbox(self):
        if self.on_login_page():
            self.login()
        self._click_nav("Fax Management")
        self._click_nav("Inbound Faxes")
        if "/faxes/inbound" not in self.page.url:
            raise PortalError(f"expected inbox, at {self.page.url}")

    def inventory(self) -> list[dict]:
        """Walk every inbox page with status All and no search."""
        self.open_inbox()
        p = self.page
        p.fill("#search", "")
        p.select_option("#status", "all")
        p.get_by_role("button", name="Filter").click()
        p.wait_for_load_state()
        rows, seen_pages = {}, 0
        while True:
            seen_pages += 1
            for tr in p.locator("table[aria-label='Inbound faxes'] tbody tr").all():
                tds = [t.inner_text().strip() for t in tr.locator("td").all()]
                if len(tds) < 5:
                    continue
                rows[tds[0]] = {"fax_id": tds[0], "received_at": tds[1], "sender": tds[2],
                                "ui_pages": int(tds[3]), "status": tds[4]}
            nxt = p.locator(".pagination").get_by_role("link", name="Next")
            if nxt.count() == 0 or seen_pages > 100:
                break
            nxt.click()
            p.wait_for_load_state()
            self.relogin_if_needed()
        total = int(re.search(r"(\d+) transmissions", p.locator(".pagination").inner_text()).group(1))
        if total != len(rows):
            raise PortalError(f"inventory saw {len(rows)} faxes, portal reports {total}")
        return list(rows.values())

    def open_fax(self, fax_id: str):
        """Inbox -> search exact ID -> click the exact link -> assert detail page."""
        for _ in range(3):
            self.open_inbox()
            p = self.page
            p.fill("#search", fax_id)
            p.get_by_role("button", name="Filter").click()
            p.wait_for_load_state()
            if self.relogin_if_needed():
                continue
            link = p.locator("table[aria-label='Inbound faxes'] tbody td:first-child a").filter(
                has_text=re.compile(rf"^{re.escape(fax_id)}$"))
            if link.count() != 1:
                raise PortalError(f"{fax_id}: expected one exact row, found {link.count()}")
            link.click()
            p.wait_for_load_state()
            if self.relogin_if_needed():
                continue
            if p.locator("h1").inner_text().strip() != f"Fax {fax_id}":
                raise PortalError(f"{fax_id}: detail page shows {p.locator('h1').inner_text()!r}")
            return
        raise PortalError(f"{fax_id}: could not reach detail page")

    def read_status(self, fax_id: str) -> dict:
        """Fresh navigation to the fax; returns status and saved routing fields if routed."""
        self.open_fax(fax_id)
        kv = self._kv(self.page.locator("dl.kv").last)
        info = self._kv(self.page.locator("dl.kv").first)
        if info.get("Fax ID") != fax_id:
            raise PortalError(f"{fax_id}: transmission panel shows {info.get('Fax ID')!r}")
        if info.get("Status") == "Routed":
            return {"status": "Routed", "assignee": kv.get("Assigned To"),
                    "doc_type": kv.get("Document Type"), "folder": kv.get("Document Folder")}
        return {"status": info.get("Status")}

    @staticmethod
    def _kv(dl) -> dict:
        dts = [x.inner_text().strip() for x in dl.locator("dt").all()]
        dds = [x.inner_text().strip() for x in dl.locator("dd").all()]
        return dict(zip(dts, dds))

    def download(self, fax_id: str, dest: Path) -> Path:
        """Clicks Download Fax PDF once. Raises on an error page instead of a file."""
        self.open_fax(fax_id)
        p = self.page
        try:
            with p.expect_download(timeout=8_000) as dl:
                p.locator("#download-fax").click()
            d = dl.value
            if d.failure():
                raise PortalError(f"download failed: {d.failure()}")
            tmp = dest.with_suffix(".part")
            d.save_as(tmp)
            return tmp
        except PWTimeout:
            p.wait_for_load_state()
            if self.on_login_page():
                raise PortalError("session expired during download")
            alert = p.locator("[role=alert]")
            shown = alert.first.inner_text() if alert.count() else p.locator("main").inner_text()[:160]
            raise PortalError(f"no file delivered; page says: {shown!r}")

    def routed_documents(self) -> dict:
        self._click_nav("Routed Documents")
        rows = {}
        for tr in self.page.locator("table[aria-label='Routed documents'] tbody tr").all():
            tds = [t.inner_text().strip() for t in tr.locator("td").all()]
            if len(tds) >= 4:
                rows[tds[0]] = {"doc_type": tds[1], "assignee": tds[2], "folder": tds[3]}
        return rows

    def submit_route(self, fax_id: str, fields: dict, note: str, before_ok=lambda: None) -> str:
        """Fills and submits the route form. Returns a description of what the page showed;
        the caller must verify through read_status, never trust this."""
        self.open_fax(fax_id)
        p = self.page
        p.get_by_role("link", name="Route / Assign Fax").click()
        p.wait_for_load_state()
        self.relogin_if_needed()
        if p.locator("h1").inner_text().strip() != f"Route fax {fax_id}":
            raise PortalError(f"{fax_id}: route form not shown ({p.locator('h1').inner_text()!r})")
        p.select_option("#assignee", label=fields["assignee"])
        p.select_option("#doc_type", label=fields["doc_type"])
        p.select_option("#folder", label=fields["folder"])
        p.fill("#notes", note)
        for sel, want in (("#assignee", "assignee"), ("#doc_type", "doc_type"), ("#folder", "folder")):
            got = p.locator(sel).evaluate("e => e.selectedOptions[0].text")
            if got != fields[want]:
                raise PortalError(f"{fax_id}: {sel} shows {got!r}, wanted {fields[want]!r}")
        p.get_by_role("button", name="Review Routing").click()
        modal = p.locator("#confirmation.show")
        modal.wait_for(state="visible")
        summary = self._kv(modal.locator("dl.kv"))
        expect = {"Fax": fax_id, "Assigned To": fields["assignee"],
                  "Document Type": fields["doc_type"], "Document Folder": fields["folder"]}
        if summary != expect:
            raise PortalError(f"{fax_id}: confirmation shows {summary}, expected {expect}")
        before_ok()
        try:
            with p.expect_navigation(timeout=15_000):
                p.locator("#confirm-ok").click()
        except PWTimeout:
            return "timeout"
        if self.on_login_page():
            return "login_page"
        alert = p.locator("[role=alert]")
        return alert.first.inner_text().strip() if alert.count() else p.url


def wait_ready(timeout=10):
    import urllib.request
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(BASE_URL + "/login", timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise PortalError(f"portal not reachable at {BASE_URL}")
