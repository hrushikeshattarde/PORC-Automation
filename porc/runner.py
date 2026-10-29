"""Orchestration: find delivered Walmart c/o SDI MRO loads, build the Zeus order, submit it,
download the PO/Rate Confirmation PDF, and attach it to the TPro load as 'Rate Confirmation'."""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .cloud import S3Sync
from .config import Settings
from .docs import verify_bol_pdf
from .mapper import ZeusOrder, build_order, parse_ts, ship_to_code, store_number_from_code
from .state import Ledger
from .tpro_client import TProClient, TProError
from .tpro_db import TProDB, TProDBError
from .zeus_portal import ZeusPortal, ZeusStepError

log = logging.getLogger("porc")


@dataclass
class Result:
    load_id: int
    work_order: str
    action: str            # planned | submitted | uploaded | skipped | failed
    detail: str = ""
    pdf_path: str = ""
    tpro_file_id: str = ""


def setup_logging(logs_dir: Path, verbose: bool) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(logs_dir / f"porc_{date.today():%Y%m%d}.log", encoding="utf-8")]
    # force=True replaces the handler AWS Lambda pre-installs on the root logger
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt, handlers=handlers, force=True)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def describe(order: ZeusOrder, ship_to: str | None = None) -> str:
    lines = [
        f"  Work Order #................ {order.work_order}",
        f"  Requested By ............... {order.requested_by[:140]}{'…' if len(order.requested_by) > 140 else ''}",
        f"  Expected Delivery Date ..... {order.expected_delivery_date:%m/%d/%Y} (today)",
        f"  Ship To (from Install Loc) . {ship_to or '<read from Zeus after entering WO#>'}",
        f"  Note To Customer ........... {order.note_to_customer[:140].replace(chr(10), ' | ')}{'…' if len(order.note_to_customer) > 140 else ''}",
        f"  Mfg. Name (shipper) ........ {order.mfg_name}",
        f"  Mfg. Part # (WO#) .......... {order.mfg_part}",
        f"  Supplier Part # (Load ID) .. {order.supplier_part}",
        f"  Description (consignee) .... {order.description}",
        f"  Unit Price (freight bill) .. ${order.unit_price:,.2f}",
        f"  BOL file ................... {order.bol_file_name} (TPro file id {order.bol_file_id}); delivery cutoff {order.delivery_cutoff}",
    ]
    for w in order.warnings:
        lines.append(f"  ! {w}")
    return "\n".join(lines)


def go_live_date(settings: Settings) -> date | None:
    if not settings.start_date:
        return None
    try:
        return date.fromisoformat(settings.start_date)
    except ValueError:
        log.warning("PORC_START_DATE %r is not YYYY-MM-DD; ignoring it", settings.start_date)
        return None


def delivery_date(load: dict) -> date | None:
    """Scheduled consignee delivery date (the field TPro's delivery-date search filters on)."""
    cn = next((w for w in (load.get("waypoints") or []) if (w or {}).get("type") == "CN"), {}) or {}
    ts = parse_ts((cn.get("appointmentTime") or {}).get("open"))
    return ts.date() if ts else None


def candidate_loads(tpro: TProClient, settings: Settings, lookback_days: int, load_ids: list[int]) -> list[dict]:
    if load_ids:
        return [tpro.get_load(i) for i in load_ids]
    end = date.today()
    start = end - timedelta(days=min(lookback_days, 59))
    live_from = go_live_date(settings)
    if live_from and live_from > start:
        start = live_from
    log.info("searching delivered loads with delivery date %s .. %s%s", start, end,
             f" (go-live date {live_from})" if live_from else "")
    loads = tpro.search_delivered_loads(settings.customer_id, start.isoformat(), end.isoformat())
    loads = [l for l in loads if ((l.get("billingInfo") or {}).get("customerId") == settings.customer_id)]
    loads.sort(key=lambda l: l.get("lastUpdated") or "", reverse=True)
    return loads


def process_load(load: dict, *, tpro: TProClient, settings: Settings, ledger: Ledger, zeus: ZeusPortal | None,
                 tpro_db: TProDB | None, dry_run: bool, no_submit: bool, pause_before_submit: bool,
                 skip_upload: bool = False) -> Result:
    load_id = int(load["id"])
    status = (load.get("status") or {}).get("loadStatus", "")
    if status.lower() != "delivered":
        return Result(load_id, "", "skipped", f"load status is '{status}', not Delivered")

    live_from = go_live_date(settings)
    delivered = delivery_date(load)
    if live_from and delivered and delivered < live_from:
        return Result(load_id, "", "skipped", f"delivered {delivered}, before go-live date {live_from}")

    prior = ledger.get(load_id) or {}
    if prior.get("stage") == "uploaded":
        return Result(load_id, prior.get("work_order", ""), "skipped", "already completed (ledger)")

    files = tpro.list_files(load_id)
    if any(f.get("fileTypeId") == settings.rate_confirmation_doc_type_id for f in files):
        ledger.set(load_id, stage="uploaded", note="Rate Confirmation already present in TPro")
        return Result(load_id, "", "skipped", "Rate Confirmation already attached in TPro")

    detail = tpro.get_load(load_id)
    dispatches = tpro.get_dispatches(load_id)
    internal_comments = None
    if tpro_db is not None:
        try:
            internal_comments = tpro_db.get_internal_comments(load_id)
        except TProDBError as exc:
            log.warning("load %s: could not read Internal Comments from the TPro database: %s", load_id, exc)
    order = build_order(detail, files, dispatches, requested_by_default=settings.requested_by_default,
                        internal_comments=internal_comments, min_freight=settings.min_freight)
    if not order.ok:
        ledger.set(load_id, stage="skipped", work_order=order.work_order, reasons=order.skip_reasons)
        return Result(load_id, order.work_order, "skipped", "; ".join(order.skip_reasons))

    log.info("Load %s -> Zeus order plan:\n%s", load_id, describe(order))
    if dry_run:
        return Result(load_id, order.work_order, "planned", "; ".join(order.warnings))

    if zeus is None:
        raise RuntimeError("Zeus portal not initialised")

    pdf_path = Path(prior.get("pdf_path") or (settings.downloads_dir / f"zeus_rc_{load_id}_WO{order.work_order}.pdf"))
    stage = prior.get("stage")

    # ---- Stage 1: create + submit the Zeus order (unless a previous run already did) --------------
    if stage not in ("zeus_submitted", "pdf_downloaded"):
        bol_path = settings.downloads_dir / f"bol_{load_id}_{order.bol_file_id}.pdf"
        if bol_path.exists():
            bol_path.unlink()                      # always fetch a fresh copy; never trust a leftover file
        tpro.download_file(order.bol_file_id, bol_path)
        bol_ok, bol_detail = verify_bol_pdf(bol_path, load_id, order.work_order)
        if not bol_ok:
            ledger.set(load_id, stage="skipped", work_order=order.work_order, reasons=[f"BOL check failed: {bol_detail}"])
            return Result(load_id, order.work_order, "skipped", f"BOL check failed: {bol_detail}")
        log.info("Load %s BOL verified: %s", load_id, bol_detail)
        if not zeus.logged_in:
            zeus.login()
        zeus.go_to_orders()
        if zeus.open_existing_draft(order.work_order):
            # A draft left by an interrupted run may hold a half-filled item; rebuild from scratch instead.
            log.warning("Load %s: deleting a leftover Zeus draft for WO %s and starting over", load_id, order.work_order)
            zeus.discard_draft()
        # Guard: an order for this WO# and amount may already exist (e.g. created by hand). A Zeus order cannot
        # be deleted, so never create a second one: attach the existing order's PDF to TPro instead.
        existing = zeus.existing_pending_order(order.work_order, order.unit_price)
        if existing:
            existing_no, existing_row = existing
            log.warning("Load %s: Zeus already has pending order %s for WO %s at $%s; reusing it instead of "
                        "creating a duplicate", load_id, existing_no, order.work_order, f"{order.unit_price:,.2f}")
            zeus.download_pdf_from_row(existing_row, existing_no, pdf_path)
            ledger.set(load_id, stage="pdf_downloaded", work_order=order.work_order, unit_price=order.unit_price,
                       zeus_order_no=existing_no, pdf_path=str(pdf_path), bol_file_id=order.bol_file_id,
                       reused_existing_order=True)
            if skip_upload:
                return Result(load_id, order.work_order, "downloaded",
                              f"existing Zeus order {existing_no} found; PDF saved locally; TPro upload skipped", str(pdf_path))
            comments = (f"Zeus order {existing_no} / Rate Confirmation - WO# {order.work_order} - Load #{load_id} "
                        f"(pre-existing Zeus order, attached by PORC bot)")
            resp = tpro.upload_file(load_id, pdf_path, settings.rate_confirmation_doc_type, comments)
            file_id = str(((resp or {}).get("result") or {}).get("id") or (resp or {}).get("id") or resp)
            if str((resp or {}).get("STATUS", "SUCCESS")).upper() != "SUCCESS":
                raise TProError(200, f"upload not confirmed: {resp}", "POST /files/upload")
            ledger.set(load_id, stage="uploaded", tpro_upload=resp)
            return Result(load_id, order.work_order, "uploaded",
                          f"existing Zeus order {existing_no} (not created by the bot); its PDF attached to TPro",
                          str(pdf_path), file_id)
        install_location = zeus.start_order(order.work_order)
        raw_code, ship_to = ship_to_code(install_location)
        if not ship_to:
            zeus.snapshot(f"install_location_unparsed_{load_id}")
            raise ZeusStepError("install_location", f"could not read a facility code from Install Location {install_location!r}")
        zeus_store = store_number_from_code(ship_to)
        if order.consignee_store_number is not None and zeus_store is not None and zeus_store != order.consignee_store_number:
            msg = (f"Zeus Install Location {install_location!r} (store {zeus_store}) does not match TPro consignee "
                   f"{order.consignee_name!r} (store {order.consignee_store_number})")
            if settings.strict_store_match:
                zeus.discard_draft()
                ledger.set(load_id, stage="skipped", work_order=order.work_order, reasons=[msg])
                return Result(load_id, order.work_order, "skipped", msg)
            log.warning("%s - continuing because PORC_STRICT_STORE_MATCH is false", msg)
        if raw_code != ship_to:
            log.info("Sam's Club rule applied: Install Location code %s -> Ship To %s", raw_code, ship_to)
        requested_by = zeus.fill_order_header(order, ship_to)
        zeus.add_non_catalog_item(order, bol_path)
        total = zeus.read_order_total()
        if total is None or abs(total - order.unit_price) > 0.01:
            zeus.snapshot(f"total_mismatch_{load_id}")
            raise ZeusStepError("verify_total", f"Zeus Order Total {total} != freight bill ${order.unit_price:,.2f}")
        if no_submit:
            zeus.snapshot(f"no_submit_{load_id}")
            zeus.discard_draft()
            return Result(load_id, order.work_order, "planned",
                          f"filled in Zeus (Requested By = {requested_by}) and discarded (--no-submit)")
        if pause_before_submit:
            input(f"\n>>> Load {load_id} / WO {order.work_order} is filled in Zeus. Press Enter to SUBMIT, Ctrl+C to abort... ")
        draft_url = zeus.order_url
        ledger.set(load_id, stage="zeus_submitted", work_order=order.work_order, ship_to=ship_to,
                   install_location=install_location, unit_price=order.unit_price, bol_file_id=order.bol_file_id,
                   requested_by=requested_by, draft_url=draft_url, submit_clicked=False)
        returned = zeus.submit_order()
        ledger.set(load_id, submit_clicked=True, returned_to_list=returned)
        stage = "zeus_submitted"
        prior = ledger.get(load_id) or {}

    # ---- Stage 2: download the PO / rate confirmation PDF -----------------------------------------
    if stage == "zeus_submitted" or not pdf_path.exists():
        if not zeus.logged_in:
            zeus.login()
        amount = float(prior.get("unit_price") or order.unit_price)
        _, order_no = zeus.download_order_pdf(order.work_order, amount, pdf_path)
        ledger.set(load_id, stage="pdf_downloaded", pdf_path=str(pdf_path), zeus_order_no=order_no)
        prior = ledger.get(load_id) or {}

    order_no = prior.get("zeus_order_no") or ""
    if skip_upload:
        return Result(load_id, order.work_order, "downloaded",
                      f"Zeus order {order_no} submitted; PDF saved locally; TPro upload skipped (--skip-upload)",
                      str(pdf_path))

    # ---- Stage 3: attach to the TPro load as 'Rate Confirmation' ----------------------------------
    comments = f"Zeus order {order_no} / Rate Confirmation - WO# {order.work_order} - Load #{load_id} (PORC bot)"
    resp = tpro.upload_file(load_id, pdf_path, settings.rate_confirmation_doc_type, comments)
    # API answers {"STATUS": "SUCCESS", "MESSAGE": "File uploaded", "result": {"id": 12345678}}
    file_id = str(((resp or {}).get("result") or {}).get("id") or (resp or {}).get("id") or resp)
    if str((resp or {}).get("STATUS", "SUCCESS")).upper() != "SUCCESS":
        raise TProError(200, f"upload not confirmed: {resp}", "POST /files/upload")
    ledger.set(load_id, stage="uploaded", tpro_upload=resp)
    return Result(load_id, order.work_order, "uploaded", "Zeus order submitted; PDF attached to TPro", str(pdf_path), file_id)


def health_check(settings: Settings, headless: bool) -> int:
    """--check: prove every dependency works from wherever the bot runs (TPro API, TPro DB, S3, Zeus sign-in)."""
    failures = 0

    def report(name: str, ok: bool, detail: str) -> None:
        nonlocal failures
        failures += 0 if ok else 1
        log.info("CHECK %-12s %s  %s", name, "OK " if ok else "FAIL", detail)

    try:
        tpro = TProClient(settings.tpro_base_url, settings.tpro_username, settings.tpro_password)
        n = len(tpro.search_delivered_loads(settings.customer_id, (date.today() - timedelta(days=7)).isoformat(),
                                            date.today().isoformat()))
        report("tpro-api", True, f"{n} delivered load(s) in the last 7 days")
    except Exception as exc:  # noqa: BLE001
        report("tpro-api", False, repr(exc))
    if settings.tpro_db_enabled:
        try:
            db = TProDB(settings.tpro_db_host, settings.tpro_db_port, settings.tpro_db_name,
                        settings.tpro_db_user, settings.tpro_db_password)
            value = db.get_internal_comments(2635421)
            db.close()
            report("tpro-db", value is not None, f"load 2635421 Internal Comments = {value!r}")
        except Exception as exc:  # noqa: BLE001
            report("tpro-db", False, repr(exc))
    else:
        report("tpro-db", False, "TPRO_DB_* not configured")
    s3 = S3Sync.from_env()
    if s3:
        try:
            s3.s3.head_bucket(Bucket=s3.bucket)
            report("s3", True, f"bucket {s3.bucket} reachable")
        except Exception as exc:  # noqa: BLE001
            report("s3", False, repr(exc))
    else:
        report("s3", True, "not configured (local disk)")
    try:
        with ZeusPortal(settings.zeus_url, settings.zeus_username, settings.zeus_password, headless=headless,
                        artifacts_dir=settings.discovery_dir, downloads_dir=settings.downloads_dir) as zeus:
            zeus.login()
            zeus.go_to_orders()
        report("zeus", True, f"signed in as {settings.zeus_username} and opened the Orders page")
    except Exception as exc:  # noqa: BLE001
        report("zeus", False, repr(exc))
    log.info("CHECK summary: %s", "all OK" if failures == 0 else f"{failures} failure(s)")
    return 0 if failures == 0 else 1


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Zeus PO / Rate Confirmation bot for Walmart c/o SDI MRO loads")
    p.add_argument("--check", action="store_true", help="health check: TPro API, TPro DB, S3 and Zeus sign-in; no load is touched")
    p.add_argument("--dry-run", action="store_true", help="read TPro and print the Zeus order plan; touch nothing")
    p.add_argument("--no-submit", action="store_true", help="fill the Zeus order, screenshot it, then delete the draft")
    p.add_argument("--pause-before-submit", action="store_true", help="headed mode: wait for Enter before Submit Order")
    p.add_argument("--skip-upload", action="store_true", help="submit in Zeus and save the PDF locally, but do not attach it to TPro")
    p.add_argument("--limit", type=int, default=0, help="process at most N loads (0 = all)")
    p.add_argument("--load-id", type=int, action="append", default=[], help="process only this load id (repeatable)")
    p.add_argument("--lookback-days", type=int, default=None, help="delivery-date window (default from .env)")
    p.add_argument("--headed", action="store_true", help="show the browser")
    p.add_argument("--discover", action="store_true", help="dump Zeus screens for selector verification (never submits)")
    p.add_argument("--work-order", default=None, help="with --discover: WO# to open a draft with (draft is deleted after)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    settings = Settings.load()
    settings.ensure_dirs()
    setup_logging(settings.logs_dir, args.verbose)
    headless = settings.headless and not args.headed and not args.pause_before_submit

    if args.check:
        return health_check(settings, headless)

    if args.discover:
        with ZeusPortal(settings.zeus_url, settings.zeus_username, settings.zeus_password, headless=headless,
                        artifacts_dir=settings.discovery_dir, downloads_dir=settings.downloads_dir) as zeus:
            zeus.discover(args.work_order)
        log.info("discovery artifacts written to %s", settings.discovery_dir)
        return 0

    tpro = TProClient(settings.tpro_base_url, settings.tpro_username, settings.tpro_password)
    ledger_path = settings.state_dir / "processed_loads.json"
    s3 = S3Sync.from_env()                       # only when PORC_S3_BUCKET is set (container deployments)
    if s3:
        s3.pull_ledger(ledger_path)
    ledger = Ledger(ledger_path, on_save=(lambda p: s3.push_ledger(p)) if s3 else None)
    loads = candidate_loads(tpro, settings, args.lookback_days or settings.lookback_days, args.load_id)
    if args.limit:
        loads = loads[: args.limit]
    log.info("%d candidate delivered load(s) for customer %s (%s)", len(loads), settings.customer_id, settings.customer_name)

    results: list[Result] = []
    zeus_cm = None
    zeus = None
    tpro_db = None
    try:
        if settings.tpro_db_enabled:
            tpro_db = TProDB(settings.tpro_db_host, settings.tpro_db_port, settings.tpro_db_name,
                             settings.tpro_db_user, settings.tpro_db_password)
        else:
            log.warning("TPRO_DB_* not set: Internal Comments cannot be read; "
                        "Requested By falls back to shipper-instruction names / PORC_REQUESTED_BY_DEFAULT")
        if not args.dry_run:
            zeus_cm = ZeusPortal(settings.zeus_url, settings.zeus_username, settings.zeus_password, headless=headless,
                                 artifacts_dir=settings.discovery_dir, downloads_dir=settings.downloads_dir)
            zeus = zeus_cm.__enter__()
        for load in loads:
            load_id = int(load["id"])
            try:
                res = process_load(load, tpro=tpro, settings=settings, ledger=ledger, zeus=zeus, tpro_db=tpro_db,
                                   dry_run=args.dry_run, no_submit=args.no_submit,
                                   pause_before_submit=args.pause_before_submit, skip_upload=args.skip_upload)
            except ZeusStepError as exc:
                if zeus:
                    zeus.snapshot(f"failed_{load_id}_{exc.step}")
                ledger.set(load_id, stage="failed", step=exc.step, error=str(exc))
                res = Result(load_id, "", "failed", str(exc))
            except TProError as exc:
                ledger.set(load_id, stage="failed", step="tpro", error=str(exc))
                res = Result(load_id, "", "failed", f"TPro API: {exc}")
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("unexpected error on load %s", load_id)
                if zeus:
                    zeus.snapshot(f"crash_{load_id}")
                ledger.set(load_id, stage="failed", step="unexpected", error=repr(exc))
                res = Result(load_id, "", "failed", repr(exc))
            log.info("Load %s -> %s %s", res.load_id, res.action.upper(), res.detail)
            results.append(res)
    finally:
        if zeus_cm:
            zeus_cm.__exit__(None, None, None)
        if tpro_db:
            tpro_db.close()
        if s3:
            try:
                for name in ("downloads", "discovery", "logs"):
                    s3.push_dir(getattr(settings, f"{name}_dir"), name)
            except Exception as exc:  # noqa: BLE001
                log.warning("S3 artifact sync failed: %s", exc)

    report = settings.logs_dir / f"porc_report_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(report, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(asdict(results[0]).keys()) if results else ["load_id"])
        w.writeheader()
        for r in results:
            w.writerow(asdict(r))
    counts = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    log.info("SUMMARY %s -> %s", counts, report)
    return 0 if not any(r.action == "failed" for r in results) else 1
