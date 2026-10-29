"""Map a Transport Pro load into the Zeus order fields.

Field mapping (from the TP -> Zeus sheet, plus the clarification that TP "Internal Comments"
is the shipper stop's Instructions/Directions):

  TP Reference #                       -> Zeus Work Order #
  TP shipper Instructions/Directions   -> Zeus Requested By
  Zeus Install Location code           -> Zeus Ship To   (strip trailing "S" for Sam's Club: W08151S -> W08151)
  today                                -> Zeus Expected Delivery Date
  TP Instructions/Directions           -> Zeus Note To Customer
  Work Order #                         -> Non-catalog Mfg. Part #
  TP Load ID                           -> Non-catalog Supplier Part #
  TP Shipper name                      -> Non-catalog Mfg. Name
  TP Consignee address                 -> Non-catalog Description
  TP Freight Bill (total)              -> Non-catalog Unit Price
  TP unsigned pre-delivery BOL (PDF)   -> Non-catalog file upload
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ZeusOrder:
    load_id: int
    work_order: str
    requested_by: str
    expected_delivery_date: datetime
    note_to_customer: str
    mfg_name: str
    mfg_part: str
    supplier_part: str
    description: str
    unit_price: float
    bol_file_id: int | None
    bol_file_name: str | None
    consignee_name: str
    consignee_store_number: int | None
    delivery_cutoff: datetime | None
    warnings: list[str] = field(default_factory=list)
    skip_reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.skip_reasons


# ----------------------------------------------------------------- helpers
def strip_html(text: str | None) -> str:
    """Turn TPro note HTML (<br/>, <b>, &amp;) into plain multi-line text."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?(b|i|u|p|div|span|strong|em)[^>]*>", "", text, flags=re.I)
    text = html.unescape(text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def one_line(text: str, sep: str = " / ") -> str:
    return sep.join(part.strip() for part in text.splitlines() if part.strip())


def clean_reference(ref: str | None) -> str:
    """'(123456789)' -> '123456789'; keeps letters, digits and dashes."""
    if not ref:
        return ""
    return re.sub(r"[^A-Za-z0-9\-]", "", ref).strip()


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    for candidate in (value, value.replace("T:", "T")):  # TPro sometimes emits '2026-10-19T:14:06:05Z'
        try:
            return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def waypoint(load: dict, kind: str) -> dict:
    return next((w for w in (load.get("waypoints") or []) if (w or {}).get("type") == kind), {}) or {}


def store_number_from_name(name: str | None) -> int | None:
    """'Sams Club 8151' -> 8151, 'WALMART STORE #1015' -> 1015, 'Walmart Supercenter #407' -> 407."""
    if not name:
        return None
    m = re.search(r"#\s*(\d{2,6})\b", name) or re.search(r"\b(\d{3,6})\b", name)
    return int(m.group(1)) if m else None


def extract_location_code(install_location_text: str) -> str | None:
    """'Walmart Store #2787 - W02787' -> 'W02787'; Sam's Club '... - W08151S' -> 'W08151S'."""
    codes = re.findall(r"\b([A-Z]\d{4,6}[A-Z]?)\b", install_location_text or "")
    return codes[-1] if codes else None


def ship_to_code(install_location_text: str) -> tuple[str | None, str | None]:
    """Return (raw_install_code, ship_to_code). A trailing 'S' (Sam's Club) is removed for Ship To."""
    raw = extract_location_code(install_location_text)
    if not raw:
        return None, None
    code = raw[:-1] if raw.endswith("S") else raw
    return raw, code


def store_number_from_code(code: str | None) -> int | None:
    """'W08151' -> 8151, 'W00407' -> 407."""
    if not code:
        return None
    m = re.search(r"(\d+)", code)
    return int(m.group(1)) if m else None


def format_address(loc: dict | None, include_company: bool = True) -> str:
    loc = loc or {}
    parts = []
    if include_company and loc.get("companyName"):
        parts.append(loc["companyName"].strip())
    street = " ".join(p for p in (loc.get("address"), loc.get("address2")) if p)
    if street:
        parts.append(street.strip())
    city_line = ", ".join(p for p in (loc.get("city"), loc.get("state")) if p)
    if loc.get("postalCode"):
        city_line = f"{city_line} {loc['postalCode']}".strip()
    if city_line:
        parts.append(city_line)
    return ", ".join(parts)


# ----------------------------------------------------------- BOL selection
BOL_COMMENT_RE = re.compile(r"^\s*bill\s*of\s*lading\b", re.I)
EXCLUDE_COMMENT_RE = re.compile(r"invoice|\bMC-\d|driver supplied|damaged|\bpod\b|proof of delivery", re.I)
DRIVER_SUPPLIED_TYPE_ID = 363


def select_bol(files: list[dict], cutoff: datetime | None, bol_type_id: int = 12) -> tuple[dict | None, str]:
    """Pick the unsigned BOL PDF that was on the load before delivery.

    Rules (derived from real loads on this customer):
      * must be application/pdf (driver photos are image/* and are rejected)
      * must look like a BOL: comment starts with 'Bill Of Lading' OR document type is Bill Of Lading
      * must not be a carrier-invoice attachment, a driver-supplied doc, or a POD
      * must be uploaded before the delivery cutoff (actual delivery / consignee appointment)
      * if several qualify, prefer comments starting 'Bill Of Lading' (our own upload), then the latest.
    Returns (file, reason_if_none).
    """
    candidates = []
    for f in files or []:
        if not f:
            continue
        mime = (f.get("mimeType") or "").lower()
        comment = f.get("comments") or ""
        type_id = f.get("fileTypeId")
        created = parse_ts(f.get("dateCreated"))
        if mime != "application/pdf":
            continue
        if type_id == DRIVER_SUPPLIED_TYPE_ID or EXCLUDE_COMMENT_RE.search(comment):
            continue
        looks_like_bol = bool(BOL_COMMENT_RE.search(comment)) or type_id == bol_type_id
        if not looks_like_bol:
            continue
        if cutoff and created and created >= cutoff:
            continue
        rank = 1 if BOL_COMMENT_RE.search(comment) else 0
        candidates.append((rank, created or datetime.min.replace(tzinfo=timezone.utc), f))
    if not candidates:
        return None, "no unsigned PDF Bill Of Lading uploaded before delivery"
    candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return candidates[0][2], ""


# ------------------------------------------------------- requested-by rule
# Zeus "Requested By" is a PEOPLE LOOKUP ("FIRST LAST email", substring-matched), not free text.
# TPro's load-level "Internal Comments" (public.loads.gn_comments in the TPro database; the public
# API does not return it) holds the requester. Formats seen in production: "123456789 Jane Smith", "Sam Lee",
# "REF# 234567890     PAT PORTER", and sometimes just "456789012" (no name). The runner reads it via
# porc/tpro_db.py and passes it in. Sources, in order:
#   1. TPro Internal Comments with the WO# / REF# noise stripped
#   2. a "(First Last)" name inside the shipper stop instructions
#   3. PORC_REQUESTED_BY_DEFAULT from .env
NAME_IN_PARENS_RE = re.compile(r"\(([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){1,2})\)")
# a noise token only counts when followed by '#', ':', whitespace or a digit (so "PORTER" is not "PO" + "RTER")
COMMENT_NOISE_RE = re.compile(r"\b(REF|REFERENCE|WO|W/O|PO|ORDER|WORK\s*ORDER)(?=\s*#|\s*:|\s|\d)\s*#?\s*:?\s*", re.I)


INVISIBLE_RE = re.compile(r"[​‌‍⁠﻿]")
EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+)\s*@\s*([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def name_from_internal_comments(internal_comments: str | None, work_order: str = "") -> str:
    """Lookup query for the Zeus people picker.
    '123456789 Jane Smith' -> 'Jane Smith'; 'REF# 234567890  PAT PORTER' -> 'PAT PORTER';
    '(345678901) ​ maria.lopez @example.com' -> 'maria.lopez' (the picker matches on the email too);
    '456789012' -> '' (no requester recorded)."""
    text = strip_html(internal_comments)
    if not text:
        return ""
    text = INVISIBLE_RE.sub("", text).replace("\xa0", " ")
    text = one_line(text, sep=" ")
    email = EMAIL_RE.search(text)
    if email:
        return email.group(1).strip(".").lower()           # 'maria.lopez' is unique enough and tolerant of spacing
    if work_order:
        text = text.replace(work_order, " ")
    text = re.sub(r"\b\d{5,}\b", " ", text)                 # any other long number (WO#, PO#)
    text = COMMENT_NOISE_RE.sub(" ", text)
    text = re.sub(r"[()\[\]{}]", " ", text)                  # brackets left over from '(WO#)'
    text = re.sub(r"^[\s\-:#,/|.]+|[\s\-:#,/|.]+$", "", text)  # leading/trailing separators
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text if re.search(r"[A-Za-z]{2,}", text) else ""


def requested_by_query(internal_comments: str | None, shipper_instructions: str, default: str,
                       work_order: str = "") -> tuple[str, str]:
    """Return (lookup query, source)."""
    name = name_from_internal_comments(internal_comments, work_order)
    if name:
        return name, "internal comments"
    for m in NAME_IN_PARENS_RE.finditer(shipper_instructions or ""):
        candidate = m.group(1).strip()
        if not re.search(r"\d", candidate) and candidate.upper() not in ("ASAP", "FCFS"):
            return candidate, "shipper instructions"
    return (default or "").strip(), "default"


# ---------------------------------------------------------------- builder
def build_order(load: dict, files: list[dict], dispatches: list[dict], requested_by_default: str = "",
                internal_comments: str | None = None, now: datetime | None = None,
                min_freight: float = 0.01) -> ZeusOrder:
    now = now or datetime.now()
    sh, cn = waypoint(load, "SH"), waypoint(load, "CN")
    sh_loc, cn_loc = sh.get("location") or {}, cn.get("location") or {}
    ref = load.get("reference") or {}
    charges = ((load.get("billingInfo") or {}).get("charges") or {})

    work_order = clean_reference(ref.get("referenceNumber")) or clean_reference(ref.get("billOfLading"))
    total_freight = float(charges.get("totalFreight") or 0) + float(charges.get("totalFuel") or 0)

    # Delivery cutoff: actual delivery from the dispatch if present, else the consignee appointment.
    cutoff = None
    for d in dispatches or []:
        for w in (d.get("waypoints") or []):           # TPro returns "waypoints": null on some dispatches
            if (w or {}).get("type") == "CN":
                actual = (w.get("actualTime") or w.get("actual_date") or {})
                cutoff = parse_ts(actual.get("start") or actual.get("open")) or cutoff
    if cutoff is None:
        cutoff = parse_ts((cn.get("appointmentTime") or {}).get("open"))

    bol, bol_reason = select_bol(files, cutoff)

    shipper_instructions = strip_html(sh.get("notes"))
    consignee_instructions = strip_html(cn.get("notes"))
    all_instructions = "\n".join(t for t in (shipper_instructions, consignee_instructions) if t)
    requested_by, rb_source = requested_by_query(internal_comments, shipper_instructions, requested_by_default, work_order)

    order = ZeusOrder(
        load_id=int(load["id"]),
        work_order=work_order,
        requested_by=requested_by,                         # lookup query for the Zeus people picker
        expected_delivery_date=now,
        note_to_customer=all_instructions,
        mfg_name=(sh_loc.get("companyName") or "").strip(),
        mfg_part=work_order,
        supplier_part=str(load["id"]),
        description=format_address(cn_loc),
        unit_price=round(total_freight, 2),
        bol_file_id=bol.get("id") if bol else None,
        bol_file_name=bol.get("fileName") if bol else None,
        consignee_name=cn_loc.get("companyName") or "",
        consignee_store_number=store_number_from_name(cn_loc.get("companyName")),
        delivery_cutoff=cutoff,
    )

    if not work_order:
        order.skip_reasons.append("load has no Reference # (Work Order #)")
    if not bol:
        order.skip_reasons.append(bol_reason)
    if order.unit_price < min_freight:
        # Rule: never create a Rate Confirmation for a load whose Freight Bill is $0 (or below the minimum).
        order.skip_reasons.append(f"Freight Bill is ${order.unit_price:,.2f} (below ${min_freight:,.2f}); "
                                  "no Rate Confirmation created")
    if not order.mfg_name:
        order.skip_reasons.append("shipper name missing")
    if not order.requested_by:
        order.skip_reasons.append("no Requested By person: Internal Comments unavailable, no name in shipper "
                                  "instructions, and PORC_REQUESTED_BY_DEFAULT is empty")
    elif rb_source != "internal comments":
        order.warnings.append(f"Requested By {requested_by!r} taken from {rb_source} (Internal Comments unavailable)")
    if order.consignee_store_number is None:
        order.warnings.append("consignee name has no store number; cannot cross-check Zeus Install Location")
    if len(order.mfg_name) > 50:
        order.warnings.append("shipper name longer than 50 chars; Zeus Mfg. Name will be truncated")
    return order
