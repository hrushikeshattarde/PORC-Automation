"""Drive the SDI ZEUS Supplier Portal (Blazor Server) with Playwright.

Verified against the live portal on 2026-10-28 (signed in as the Circle Logistics supplier user):

  /signin                      Email / Password / [Sign In]            -> /home shows a "Sign Out" link
  /order-list                  "Orders To Complete" table (WO# is a link to the draft) + [Start New Order ...]
                               "Pending Approval" table.table-sdi: Order# | WO# | Install/Ship To | date, Entered By |
                               Lines | Total | <span title="Download PDF">
  /order/start                 #work-order-number + [Start New Order ...]       -> /order/{id}
  /order/{id}                  "Order Details": Work Order # (disabled), #requested-by-input (people lookup),
                               Expected Delivery Date (.input-date-plus input, 10 chars, M/D/YYYY),
                               #ship-to-input (facility lookup), Install Location (disabled), Note To Customer
                               (textarea, 1333 chars), [Add Non-Catalog Item ...], "Order Total (USD): $x",
                               [Save Changes] [Save & Return] [Submit Order] [Delete Order] [Cancel]
  /order/{id}/non-catalog      Mfg. Name(50) Mfg. Part #(50) Supplier Part #(50) Description(254) Quantity
                               Unit Price(11) Unit of Measure(select, EA) Extended Price (disabled),
                               Dropzone input.dz-hidden-input (accepts PDF), [Add and Return] [Add and Continue ...] [Cancel]

Lookups (`hg-auto-complete`): typing renders `div.suggestions > div.suggestion` entries; the highlighted entry is
auto-selected on blur, so the bot always clicks the exact suggestion and then verifies the field value.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout, sync_playwright

from .mapper import ZeusOrder

log = logging.getLogger(__name__)

MONEY_RE = re.compile(r"\$\s*([\d,]+\.\d{2})")


class ZeusStepError(RuntimeError):
    def __init__(self, step: str, message: str):
        super().__init__(f"[{step}] {message}")
        self.step = step


def _money(text: str) -> float | None:
    m = MONEY_RE.search(text or "")
    return float(m.group(1).replace(",", "")) if m else None


def near_match(wanted: str, suggestion: str, threshold: float = 0.9) -> bool:
    """True when `wanted` (a name or e-mail local part) is a near-exact match of the suggestion's e-mail
    local part or display name: 'maria.lopez' ~ 'maria.lopes' (0.91) yes; 'jane.doe' ~ 'john.doe' no."""
    from difflib import SequenceMatcher

    w = wanted.strip().lower()
    parts = suggestion.strip().split()
    candidates = set()
    for p in parts:
        if "@" in p:
            candidates.add(p.split("@", 1)[0].lower())
    name = " ".join(p for p in parts if "@" not in p).lower()
    if name:
        candidates.add(name)
        candidates.add(name.replace(" ", "."))
    return any(SequenceMatcher(None, w, c).ratio() >= threshold for c in candidates)


class ZeusPortal:
    def __init__(self, url: str, username: str, password: str, *, headless: bool = True,
                 artifacts_dir: Path | None = None, downloads_dir: Path | None = None, slow_mo: int = 0,
                 default_timeout_ms: int = 30_000):
        self.url = url
        self.base = url.split("/signin")[0].rstrip("/")
        self.username = username
        self.password = password
        self.headless = headless
        self.artifacts_dir = artifacts_dir or Path("discovery")
        self.downloads_dir = downloads_dir or Path("downloads")
        self.slow_mo = slow_mo
        self.default_timeout_ms = default_timeout_ms
        self._pw = None
        self._browser = None
        self._context = None
        self.page: Page | None = None
        self.logged_in = False

    # ------------------------------------------------------------ lifecycle
    def __enter__(self) -> "ZeusPortal":
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        launch_args: list[str] = []
        if os.environ.get("PORC_IN_LAMBDA") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
            # Chromium inside AWS Lambda: no sandbox user, tiny /dev/shm, single process.
            launch_args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                           "--disable-gpu", "--single-process", "--no-zygote"]
        self._browser = self._pw.chromium.launch(headless=self.headless, slow_mo=self.slow_mo, args=launch_args)
        self._context = self._browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 1000})
        self._context.set_default_timeout(self.default_timeout_ms)
        self.page = self._context.new_page()
        self.page.on("dialog", lambda d: (log.info("browser dialog: %s", d.message), d.accept()))
        return self

    def __exit__(self, *exc) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw:
            self._pw.stop()

    # -------------------------------------------------------------- helpers
    def snapshot(self, name: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = self.artifacts_dir / f"{stamp}_{name}"
        try:
            self.page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
            base.with_suffix(".html").write_text(self.page.content(), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("snapshot %s failed: %s", name, exc)

    @staticmethod
    def _settle(ms: int = 500) -> None:
        """Blazor re-renders after a SignalR round-trip; give it a beat."""
        time.sleep(ms / 1000)

    def _control(self, label: str, step: str) -> Locator:
        """The input/textarea/select inside the `.plus-control` wrapper whose label text starts with `label`."""
        wrapper = self.page.locator(".plus-control", has=self.page.locator("label", has_text=label))
        if wrapper.count() == 0:
            raise ZeusStepError(step, f"no form control labelled '{label}'")
        return wrapper.first.locator("input:not([type=hidden]):not(.qs-overlay-year), textarea, select").first

    def _fill(self, label: str, value: str, step: str, max_len: int | None = None) -> None:
        if max_len and len(value) > max_len:
            log.warning("%s: value truncated to %d chars", label, max_len)
            value = value[:max_len]
        loc = self._control(label, step)
        loc.click()
        loc.fill(value)
        loc.press("Tab")            # Blazor @bind commits on change (blur)
        self._settle()
        got = loc.input_value()
        if value and not got.strip():
            raise ZeusStepError(step, f"'{label}' did not accept the value")

    def _button(self, name: str | re.Pattern, step: str) -> Locator:
        loc = self.page.get_by_role("button", name=name)
        if loc.count() == 0:
            raise ZeusStepError(step, f"button '{name}' not found")
        return loc.first

    def _click(self, name: str | re.Pattern, step: str) -> None:
        self._button(name, step).click()
        self._settle(800)

    def _maybe_click(self, name: str | re.Pattern, timeout_ms: int = 2500) -> bool:
        loc = self.page.get_by_role("button", name=name)
        try:
            loc.first.wait_for(state="visible", timeout=timeout_ms)
            loc.first.click()
            self._settle(800)
            return True
        except PWTimeout:
            return False

    def _expect_text(self, pattern: re.Pattern, step: str, timeout_ms: int | None = None) -> None:
        try:
            self.page.get_by_text(pattern).first.wait_for(state="visible", timeout=timeout_ms or self.default_timeout_ms)
        except PWTimeout as exc:
            self.snapshot(f"missing_{step}")
            raise ZeusStepError(step, f"expected to see '{pattern.pattern}' on {self.page.url}") from exc

    def _settled_suggestions(self, suggestions: Locator) -> list[str]:
        """Read the suggestion list only once two consecutive reads agree (the Blazor list re-renders after
        every debounce while the user is typing)."""
        previous: list[str] | None = None
        for _ in range(6):
            self._settle(700)
            current = [t.strip() for t in suggestions.all_inner_texts()]
            if previous is not None and current == previous:
                return current
            previous = current
        return previous or []

    def _pick_suggestion(self, input_id: str, query: str, must_contain: str, step: str, _retry: bool = True) -> str:
        """Type into an hg-auto-complete lookup and click the suggestion containing `must_contain`.

        The Blazor list re-renders after its last debounce, so the list is read only after it settles and
        the click is retried once. A typo in TPro's Internal Comments ('maria.lopez' vs Zeus
        'maria.lopes') is tolerated only when Zeus returns exactly ONE near-exact suggestion; the lookup
        is then redone with that suggestion's own e-mail local part so the exact-match path selects it."""
        field = self.page.locator(f"#{input_id}")
        field.click()
        field.fill("")
        field.press_sequentially(query, delay=60)
        suggestions = self.page.locator("div.suggestions div.suggestion")
        try:
            suggestions.first.wait_for(state="visible", timeout=10_000)
        except PWTimeout as exc:
            self.snapshot(f"{input_id}_no_suggestions")
            raise ZeusStepError(step, f"lookup '{input_id}' showed no suggestions for {query!r}") from exc
        texts = self._settled_suggestions(suggestions)
        exact = [t for t in texts if must_contain.lower() in t.lower()]
        if not exact:
            if _retry:
                # Typo tolerance: Zeus matches on substrings, so a misspelt name/e-mail yields nothing for the full
                # query. Re-query with its first two thirds, keep the near-exact (>=90%) candidates, and accept
                # only when exactly one remains; then select it via the exact path using its own e-mail local part.
                short = query[: max(6, len(query) * 2 // 3)]
                field.click()
                field.fill("")
                field.press_sequentially(short, delay=60)
                try:
                    suggestions.first.wait_for(state="visible", timeout=10_000)
                except PWTimeout:
                    pass
                shortlist = self._settled_suggestions(suggestions)
                near = [t for t in shortlist if near_match(must_contain, t)]
                if len(near) == 1:
                    local = next((p.split("@", 1)[0] for p in near[0].split() if "@" in p), "")
                    new_query = local or " ".join(p for p in near[0].split() if "@" not in p)
                    log.warning("%s: no exact match for %r; single near-exact candidate %r (from query %r) -> "
                                "selecting via %r", input_id, must_contain, near[0], short, new_query)
                    return self._pick_suggestion(input_id, new_query, new_query, step, _retry=False)
                self.snapshot(f"{input_id}_no_match")
                raise ZeusStepError(step, f"no suggestion for {query!r} contains {must_contain!r}; full query gave "
                                          f"{texts[:8]}, shortened {short!r} gave {shortlist[:8]} "
                                          f"({len(near)} near-exact)")
            self.snapshot(f"{input_id}_no_match")
            raise ZeusStepError(step, f"no suggestion for {query!r} contains {must_contain!r}; got {texts[:8]}")
        if len(exact) > 1:
            log.warning("%s: %d suggestions match %r; taking the first", input_id, len(exact), must_contain)
        chosen = exact[0]
        target = suggestions.filter(has_text=re.compile(re.escape(must_contain), re.I)).first
        try:
            target.click(timeout=10_000)
        except PWTimeout:
            if _retry:
                log.warning("%s: suggestion list re-rendered before the click; retrying the lookup once", input_id)
                return self._pick_suggestion(input_id, query, must_contain, step, _retry=False)
            self.snapshot(f"{input_id}_click_timeout")
            raise ZeusStepError(step, f"could not click the suggestion {chosen!r} in '{input_id}'")
        self._settle()
        value = field.input_value().strip()
        if must_contain.lower() not in value.lower():
            raise ZeusStepError(step, f"lookup '{input_id}' ended with {value!r}, expected {chosen!r}")
        return value

    # ---------------------------------------------------------------- flows
    def login(self) -> None:
        step = "login"
        self.page.goto(self.url, wait_until="domcontentloaded")
        password = self.page.locator("input[type=password]").first
        try:
            password.wait_for(state="visible", timeout=60_000)     # Blazor renders the form after its runtime loads
        except PWTimeout as exc:
            self.snapshot("login_form_missing")
            raise ZeusStepError(step, "Zeus sign-in form did not render") from exc
        self._settle(500)
        email = self.page.locator("input[type=email]")
        if email.count() == 0:
            email = self.page.locator("form input[type=text], form input:not([type])")
        email.first.fill(self.username)
        password.fill(self.password)
        self.page.get_by_role("button", name=re.compile(r"^\s*Sign In\s*$", re.I)).click()
        try:
            self.page.get_by_role("link", name=re.compile(r"Sign Out", re.I)).first.wait_for(timeout=self.default_timeout_ms)
        except PWTimeout as exc:
            self.snapshot("login_failed")
            raise ZeusStepError(step, "sign-in did not complete (credentials / error banner?)") from exc
        self.logged_in = True
        log.info("Zeus: signed in as %s", self.username)

    def go_to_orders(self) -> None:
        self.page.goto(f"{self.base}/order-list", wait_until="domcontentloaded")
        self._expect_text(re.compile(r"Start New Order", re.I), "orders")
        self._settle(1000)

    def open_existing_draft(self, work_order: str) -> bool:
        """A previous run may have left a draft in 'Orders To Complete' (its WO# is a link). Reuse it."""
        section = self.page.locator("section, div", has=self.page.get_by_text(re.compile(r"Orders To Complete"))).first
        link = section.get_by_role("link", name=re.compile(rf"^\s*{re.escape(work_order)}\s*$"))
        if link.count() == 0:
            return False
        link.first.click()
        self._expect_text(re.compile(r"Order Details"), "open_draft")
        self._settle(800)
        log.info("Zeus: reopened existing draft %s for WO %s", self.page.url, work_order)
        return True

    def start_order(self, work_order: str) -> str:
        step = "start_order"
        self._click(re.compile(r"Start New Order", re.I), step)
        self.page.wait_for_url(re.compile(r"/order/start"), timeout=self.default_timeout_ms)
        wo = self.page.locator("#work-order-number")
        wo.fill(work_order)
        wo.press("Tab")
        self._click(re.compile(r"Start New Order", re.I), step)
        try:
            self.page.wait_for_url(re.compile(r"/order/\d+$"), timeout=self.default_timeout_ms)
        except PWTimeout as exc:
            self.snapshot("start_order_failed")
            raise ZeusStepError(step, f"Zeus did not open an order for WO {work_order} (invalid WO#?)") from exc
        self._expect_text(re.compile(r"Order Details"), step)
        self._settle(800)
        install = self.read_install_location()
        log.info("Zeus: draft %s for WO %s; Install Location = %r", self.page.url, work_order, install)
        return install

    @property
    def order_url(self) -> str:
        return self.page.url

    def read_install_location(self) -> str:
        return self._control("Install Location", "install_location").input_value().strip()

    def fill_order_header(self, order: ZeusOrder, ship_to: str) -> str:
        """Returns the Requested By value that was selected."""
        step = "order_header"
        try:
            requested = self._pick_suggestion("requested-by-input", order.requested_by, order.requested_by, step)
        except ZeusStepError:
            last_name = order.requested_by.split()[-1]
            if last_name.lower() == order.requested_by.lower():
                raise
            log.info("Requested By: no match for the full name; retrying the lookup with %r", last_name)
            requested = self._pick_suggestion("requested-by-input", last_name, order.requested_by, step)
        d = order.expected_delivery_date
        date_field = self._control("Expected Delivery Date", step)
        date_field.click()
        date_field.fill(f"{d.month}/{d.day}/{d.year}")
        date_field.press("Tab")
        self.page.keyboard.press("Escape")       # closes the date-picker overlay if it opened
        self._settle()
        self._pick_suggestion("ship-to-input", ship_to, ship_to, step)
        if order.note_to_customer:
            self._fill("Note To Customer", order.note_to_customer, step, max_len=1333)
        self.snapshot(f"order_header_{order.load_id}")
        return requested

    def add_non_catalog_item(self, order: ZeusOrder, bol_path: Path) -> None:
        step = "non_catalog_item"
        self._click(re.compile(r"Add Non-Catalog Item", re.I), step)
        self._maybe_click(re.compile(r"^\s*Yes\s*$", re.I))     # "save your changes before leaving?"
        self.page.wait_for_url(re.compile(r"/non-catalog"), timeout=self.default_timeout_ms)
        self._settle(800)
        self._fill("Mfg. Name", order.mfg_name, step, max_len=50)
        self._fill("Mfg. Part #", order.mfg_part, step, max_len=50)
        self._fill("Supplier Part #", order.supplier_part, step, max_len=50)
        self._fill("Description", order.description, step, max_len=254)
        self._fill("Unit Price", f"{order.unit_price:.2f}", step)
        ext = _money(self._control("Extended Price", step).input_value()) or 0.0
        if abs(ext - order.unit_price) > 0.01:
            # Extended Price shows 5 decimals without a $ sign; fall back to a plain float parse
            try:
                ext = float(self._control("Extended Price", step).input_value().replace(",", ""))
            except ValueError:
                ext = 0.0
        if abs(ext - order.unit_price) > 0.01:
            self.snapshot(f"extended_price_{order.load_id}")
            raise ZeusStepError(step, f"Extended Price {ext} != unit price {order.unit_price}")
        with open(bol_path, "rb") as fh:          # last line of defence right before the upload
            if fh.read(5) != b"%PDF-":
                raise ZeusStepError(step, f"{bol_path.name} is not a PDF; refusing to attach it")
        self.page.locator("input.dz-hidden-input").set_input_files(str(bol_path))
        self._wait_for_upload(bol_path, step)
        if self.page.locator(".dz-preview.dz-error").count():
            raise ZeusStepError(step, "Zeus reported an upload error for the BOL")
        self.snapshot(f"non_catalog_{order.load_id}")
        self._click(re.compile(r"^\s*Add and Return\s*$", re.I), step)
        self.page.wait_for_url(re.compile(r"/order/\d+$"), timeout=self.default_timeout_ms)
        self._expect_text(re.compile(r"Order Details"), step)
        self._settle(1000)

    def _wait_for_upload(self, path: Path, step: str) -> None:
        """Dropzone shows a preview per file; require BOTH its success state and the file name in the
        attached-files list before continuing (verified live: tick mark + 'bol_<load>_<file>.pdf' row)."""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            success = self.page.locator(".dz-preview.dz-success, .dz-preview.dz-complete").count() > 0
            listed = self.page.get_by_text(path.name, exact=False).count() > 0
            if success and listed:
                self._settle(1000)
                return
            if self.page.locator(".dz-preview.dz-error").count():
                self.snapshot("upload_error")
                raise ZeusStepError(step, "Zeus rejected the BOL upload (dz-error)")
            time.sleep(1)
        self.snapshot("upload_not_confirmed")
        raise ZeusStepError(step, f"could not confirm that {path.name} uploaded")

    def read_order_total(self) -> float | None:
        text = self.page.get_by_text(re.compile(r"Order Total")).first.inner_text()
        return _money(text)

    def submit_order(self) -> bool:
        """Click Submit Order and handle its prompts. Returns True when Zeus went back to the order list.
        Never raises after the click: the caller records the submit and verifies it via the Pending
        Approval list, so a re-run can never create a second order."""
        step = "submit_order"
        self.snapshot("before_submit")
        self._click(re.compile(r"^\s*Submit Order\s*$", re.I), step)
        self._maybe_click(re.compile(r"^\s*Yes\s*$", re.I))
        # Address-validation prompt seen in the markup: "Use Original Address" / "Use Suggested Address"
        radio = self.page.get_by_role("radio", name=re.compile(r"Use Original Address", re.I))
        try:
            radio.first.wait_for(state="visible", timeout=2500)
            log.info("address validation prompt shown; keeping the original address")
            self.snapshot("address_prompt")
            radio.first.check()
            self._maybe_click(re.compile(r"^(OK|Continue|Submit|Confirm|Yes)\b", re.I))
        except PWTimeout:
            pass
        returned = True
        try:
            self.page.wait_for_url(re.compile(r"/order-list"), timeout=20_000)
        except PWTimeout:
            returned = False
            log.warning("after Submit Order the page is still %s; will verify via Pending Approval", self.page.url)
        self._settle(1500)
        self.snapshot("after_submit")
        return returned

    def discard_draft(self) -> None:
        """Used by --no-submit / cleanup: leave nothing behind in 'Orders To Complete'."""
        if not re.search(r"/order/\d+", self.page.url):
            return
        if self._maybe_click(re.compile(r"^\s*Delete Order\s*$", re.I)):
            self._maybe_click(re.compile(r"^\s*(Yes|OK|Delete)\s*$", re.I))
        self._settle(1000)

    def existing_pending_order(self, work_order: str, amount: float) -> tuple[str, Locator] | None:
        """Newest Pending Approval row for this WO# whose Total equals `amount`, or None.
        Used both to find the order the bot just submitted and, before creating one, to detect an order
        somebody already created by hand for the same work order and amount (a duplicate cannot be deleted)."""
        self.go_to_orders()
        wanted = f"${amount:,.2f}"
        rows = self.page.locator("table.table-sdi tr").filter(has_text=work_order).filter(has_text=wanted)
        n = rows.count()
        if n == 0:
            return None
        if n > 1:
            log.warning("Zeus: %d Pending Approval rows for WO %s with total %s; using the newest", n, work_order, wanted)
        row = rows.first
        order_no = row.locator("td").first.inner_text().strip().split()[0]
        return order_no, row

    def find_pending_order(self, work_order: str, amount: float) -> tuple[str, Locator]:
        found = self.existing_pending_order(work_order, amount)
        if found is None:
            self.snapshot("pending_row_missing")
            raise ZeusStepError("find_order", f"no Pending Approval row for WO {work_order} with total ${amount:,.2f}")
        return found

    def download_order_pdf(self, work_order: str, amount: float, dest: Path) -> tuple[Path, str]:
        order_no, row = self.find_pending_order(work_order, amount)
        return self.download_pdf_from_row(row, order_no, dest), order_no

    def download_pdf_from_row(self, row: Locator, order_no: str, dest: Path) -> Path:
        step = "download_pdf"
        control = row.locator("span[title='Download PDF'], [title*='PDF' i]")
        if control.count() == 0:
            self.snapshot("pdf_control_missing")
            raise ZeusStepError(step, f"row for {order_no} has no 'Download PDF' control")
        with self.page.expect_download(timeout=90_000) as dl:
            control.first.click()
        download = dl.value
        dest.parent.mkdir(parents=True, exist_ok=True)
        download.save_as(str(dest))
        if dest.stat().st_size < 1000:
            raise ZeusStepError(step, f"downloaded PDF is suspiciously small ({dest.stat().st_size} bytes)")
        log.info("Zeus: order %s PDF saved -> %s", order_no, dest)
        return dest

    # ------------------------------------------------------------- discovery
    def discover(self, work_order: str | None = None) -> None:
        """Dump screenshots + HTML of each screen. Never submits; deletes the draft it creates."""
        self.login()
        self.snapshot("01_home")
        self.go_to_orders()
        self.snapshot("02_orders")
        if not work_order:
            return
        self.start_order(work_order)
        self.snapshot("03_order_details")
        self._click(re.compile(r"Add Non-Catalog Item", re.I), "discover")
        self._maybe_click(re.compile(r"^\s*Yes\s*$", re.I))
        self._settle(1000)
        self.snapshot("04_non_catalog_item")
        self._maybe_click(re.compile(r"^\s*Cancel\s*$", re.I))
        self._settle(1000)
        self.discard_draft()
        self.snapshot("05_after_discard")
