"""Moving a bureau's history from the old PHP system (app.tarjimnebi.ge) into Office.

The input is a JSON dump written by ``scripts/tarjimnebi/extract.js``, a
read-only scraper that runs inside a signed-in browser tab. This module turns
that dump into rows: clients, translators, notaries, orders with their
documents, status history, payments in and out, expenses and internal notes.

Two rules shape it:

* **One transaction, caller commits.** The loader inserts everything and
  flushes, so every constraint is really exercised; a dry run is the same code
  followed by a rollback. What the report says is what a commit would do.
* **Money is copied, never recomputed.** Prices, costs and payments arrive as
  they were. Nothing here applies the pricing rules.

Not carried over (yet): order files and Google Drive folders (only the Drive
folder id is kept on the order), portal accounts and passwords, and comments
on orders that still had unread notes in the old system (reading them would
have marked them read there).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.domain.statuses import INITIAL_STATUS, STATUS_DEFINITIONS
from suliko.models.collaboration import OrderComment
from suliko.models.directory import (
    Client,
    ClientType,
    Notary,
    Translator,
    TranslatorLanguagePair,
)
from suliko.models.finance import (
    ClientPayment,
    ClientPaymentAllocation,
    Expense,
    NotaryPayment,
    NotaryPaymentAllocation,
    PaymentMethod,
    TranslatorPayment,
    TranslatorPaymentAllocation,
)
from suliko.models.order import (
    CopyType,
    HandoverMethod,
    Order,
    OrderDocument,
    OrderStatusEvent,
    ServiceType,
    Urgency,
)
from suliko.models.reference import DocumentType, Language
from suliko.models.tenant import Tenant

SOURCE_PREFIX = "tarjimnebi:"

_METHODS: tuple[tuple[str, PaymentMethod], ...] = (
    ("bog online", PaymentMethod.BOG_ONLINE),
    ("online", PaymentMethod.BOG_ONLINE),
    ("cash", PaymentMethod.CASH),
    ("card", PaymentMethod.CARD),
    ("transfer", PaymentMethod.BANK_TRANSFER),
    ("bank", PaymentMethod.BANK_TRANSFER),
)


def payment_method(label: str | None) -> PaymentMethod:
    low = (label or "").casefold()
    return next((m for key, m in _METHODS if key in low), PaymentMethod.OTHER)


def _clip(value: Any, size: int) -> str | None:
    text = str(value).strip() if value not in (None, "") else ""
    return text[:size] or None


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _today() -> date:
    return datetime.now(UTC).date()


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


@dataclass
class Dump:
    notaries: list[dict[str, Any]] = field(default_factory=list)
    translators: list[dict[str, Any]] = field(default_factory=list)
    clients: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    #: Every expense, from the Finances page (date, category, description, order_id, amount).
    expenses: list[dict[str, Any]] = field(default_factory=list)
    reference: dict[str, Any] = field(default_factory=dict)


def load_dump(path: Path) -> Dump:
    """Read one combined JSON file, or a folder of the receiver's per-part files."""
    parts: dict[str, Any] = {}
    if path.is_dir():
        for file in sorted(path.glob("*.json")):
            if file.name.endswith(".idmap.json"):
                continue
            parts[file.stem] = json.loads(file.read_text(encoding="utf-8"))
    else:
        parts = json.loads(path.read_text(encoding="utf-8"))
    dump = Dump(
        notaries=parts.get("notaries", []),
        translators=parts.get("translators", []),
        clients=parts.get("clients", []),
        expenses=parts.get("expenses", []),
        reference=parts.get("reference", {}),
    )
    for key in sorted(k for k in parts if k.startswith("orders_")):
        dump.orders += parts[key]
    dump.orders.sort(key=lambda o: o["id"])
    return dump


@dataclass
class Report:
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: tarjimnebi id to Office id, per kind. Written next to the dump on commit.
    idmap: dict[str, dict[int, int]] = field(default_factory=dict)

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def warn(self, message: str) -> None:
        self.warnings.append(message)


class RefusedError(Exception):
    """The database is not in a state this import can safely run against."""


async def import_dump(
    db: AsyncSession,
    tenant_id: int,
    dump: Dump,
    *,
    keep_numbers: bool,
) -> Report:
    report = Report()
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
    tz = ZoneInfo(tenant.timezone or "Asia/Tbilisi")

    def moment(value: str | None) -> datetime | None:
        if not value:
            return None
        text = value.strip().replace("T", " ")
        fmt = "%Y-%m-%d %H:%M:%S" if text.count(":") == 2 else "%Y-%m-%d %H:%M"
        return datetime.strptime(text, fmt).replace(tzinfo=tz).astimezone(UTC)

    already = (
        await db.execute(
            select(func.count()).select_from(Order).where(Order.source.like(f"{SOURCE_PREFIX}%"))
        )
    ).scalar_one()
    if already:
        raise RefusedError(
            f"{already} orders from an earlier tarjimnebi import are already here. "
            "Undo them first; this import will not run twice."
        )
    if keep_numbers:
        have = (await db.execute(select(func.count()).select_from(Order))).scalar_one() + (
            await db.execute(select(func.count()).select_from(Client))
        ).scalar_one()
        if have:
            raise RefusedError(
                "--keep-numbers needs an organisation with no orders or clients, "
                "otherwise the old numbers would collide with the new ones."
            )

    # ── Reference data ──────────────────────────────────────────────────────
    languages = {row.code: row for row in (await db.execute(select(Language))).scalars()}
    for raw_code, name in sorted((dump.reference.get("languages") or {}).items()):
        code = raw_code.lower()
        if code not in languages:
            row = Language(code=code[:5], name_en=name, name_ka=name)
            db.add(row)
            languages[code] = row
            report.add("languages created")
            report.warn(
                f"Language {code} ({name}) was not in Office; "
                f"created with the Georgian name for both."
            )

    doc_types: dict[str, DocumentType] = {}
    for known in (await db.execute(select(DocumentType))).scalars():
        doc_types[known.name_ka.casefold()] = known
        doc_types.setdefault(known.name_en.casefold(), known)
    type_by_legacy: dict[int, DocumentType] = {}
    for legacy_id, name in sorted(
        (dump.reference.get("document_types") or {}).items(), key=lambda kv: int(kv[0])
    ):
        found = doc_types.get(name.casefold())
        if found is None:
            found = DocumentType(name_en=name[:255], name_ka=name[:255])
            db.add(found)
            doc_types[name.casefold()] = found
            report.add("document types created")
            report.warn(f"Document type '{name}' was not in Office; created with multiplier 1.")
        type_by_legacy[int(legacy_id)] = found
    await db.flush()

    # ── People ──────────────────────────────────────────────────────────────
    existing_clients = (await db.execute(select(Client))).scalars().all()
    by_email = {c.email.casefold(): c for c in existing_clients if c.email}
    by_personal = {c.personal_number: c for c in existing_clients if c.personal_number}
    client_of: dict[int, Client] = {}
    fresh_clients: list[tuple[int, Client]] = []
    for raw in sorted(dump.clients, key=lambda c: c["id"]):
        email = _clip(raw.get("email"), 255)
        personal = _clip(raw.get("personal_number"), 50)
        found_client = (by_email.get(email.casefold()) if email else None) or (
            by_personal.get(personal) if personal else None
        )
        if found_client is not None:
            client_of[raw["id"]] = found_client
            report.add("clients matched to an existing one")
            continue
        kind = (raw.get("client_type") or "B2C").upper()
        source = raw.get("acquisition_source") or raw.get("acquisition_group")
        client = Client(
            name=_clip(raw.get("name"), 255) or f"Client {raw['id']}",
            client_type=ClientType(kind) if kind in ("B2B", "B2C") else ClientType.B2C,
            email=email,
            phone=_clip(raw.get("phone"), 50),
            address=_clip(raw.get("address"), 500),
            personal_number=personal,
            acquisition_source=_clip(source, 255),
        )
        if keep_numbers:
            client.number = raw["id"]
        db.add(client)
        client_of[raw["id"]] = client
        fresh_clients.append((raw["id"], client))
        report.add("clients created")

    existing_translators = (await db.execute(select(Translator))).scalars().all()
    tr_by_email = {t.email.casefold(): t for t in existing_translators if t.email}
    tr_by_phone = {_digits(t.phone): t for t in existing_translators if _digits(t.phone)}
    translator_of: dict[int, Translator] = {}
    fresh_translators: list[tuple[int, Translator, dict[str, Any]]] = []
    for raw in sorted(dump.translators, key=lambda t: t["id"]):
        email = _clip(raw.get("email"), 255)
        found_tr = (tr_by_email.get(email.casefold()) if email else None) or tr_by_phone.get(
            _digits(raw.get("phone"))
        )
        if found_tr is not None:
            translator_of[raw["id"]] = found_tr
            report.add("translators matched to an existing one")
            continue
        year = str(raw.get("experience_from") or "")
        translator = Translator(
            name=_clip(raw.get("name"), 255) or f"Translator {raw['id']}",
            phone=_clip(raw.get("phone"), 50),
            email=email,
            office_address=_clip(raw.get("office_address"), 255),
            comment=raw.get("comment"),
            experience_from=date(int(year), 1, 1) if year.isdigit() else None,
            default_rate=_money(raw["default_rate"]) if raw.get("default_rate") else None,
            bank_iban=_clip(raw.get("bank_iban"), 34),
            bank_inn=_clip(raw.get("bank_inn"), 20),
            bank_code=_clip(raw.get("bank_code"), 20),
        )
        db.add(translator)
        translator_of[raw["id"]] = translator
        fresh_translators.append((raw["id"], translator, raw))
        report.add("translators created")

    existing_notaries = (await db.execute(select(Notary))).scalars().all()
    no_by_reg = {n.registration_number: n for n in existing_notaries if n.registration_number}
    notary_of: dict[int, Notary] = {}
    for raw in sorted(dump.notaries, key=lambda n: n["id"]):
        found_no = no_by_reg.get(_clip(raw.get("registration_number"), 100) or "\0")
        if found_no is not None:
            notary_of[raw["id"]] = found_no
            report.add("notaries matched to an existing one")
            continue
        notary = Notary(
            name=_clip(raw.get("name"), 255) or f"Notary {raw['id']}",
            phone=_clip(raw.get("phone"), 50),
            email=_clip(raw.get("email"), 255),
            registration_number=_clip(raw.get("registration_number"), 100),
            office_address=_clip(raw.get("office_address"), 255),
            comment=raw.get("comment"),
            bank_iban=_clip(raw.get("bank_iban"), 34),
            bank_inn=_clip(raw.get("bank_inn"), 20),
            bank_code=_clip(raw.get("bank_code"), 20),
        )
        db.add(notary)
        notary_of[raw["id"]] = notary
        report.add("notaries created")
    await db.flush()

    for _, translator, raw in fresh_translators:
        for pair in raw.get("language_pairs") or []:
            if not pair.get("from") or not pair.get("to"):
                continue
            db.add(
                TranslatorLanguagePair(
                    translator_id=translator.id,
                    source_language=pair["from"].lower()[:5],
                    target_language=pair["to"].lower()[:5],
                    rate_per_page=_money(pair["price"]) if pair.get("price") else None,
                )
            )
            report.add("translator language pairs")

    # ── Orders ──────────────────────────────────────────────────────────────
    known_statuses = set(STATUS_DEFINITIONS)
    orders: list[tuple[dict[str, Any], Order]] = []
    for raw in dump.orders:
        owner = client_of.get(raw["client_id"])
        if owner is None:
            report.warn(
                f"Order {raw['id']}: client {raw['client_id']} is not in the dump; skipped."
            )
            report.add("orders skipped")
            continue
        methods = raw.get("handover_methods") or []
        handover = (
            HandoverMethod.DELIVERY
            if "delivery" in methods
            else HandoverMethod.PICKUP
            if "pickup" in methods
            else HandoverMethod.SCAN
        )
        history = sorted(raw.get("history") or [], key=lambda h: h.get("at") or "")
        started = moment(history[0]["at"]) if history and history[0].get("at") else None
        order = Order(
            client_id=owner.id,
            order_date=_day(raw["date"]) or _today(),
            due_date=_day(raw.get("due_date")),
            contact_info=_clip(raw.get("contact_info"), 255),
            urgency=Urgency.STANDARD,
            handover_method=handover,
            delivery_address=_clip(raw.get("delivery_address"), 500),
            delivery_cost=_money(raw.get("delivery_cost")),
            source=f"{SOURCE_PREFIX}{raw['id']}",
            google_drive_folder=_clip(raw.get("google_drive_folder"), 100),
        )
        if keep_numbers:
            order.number = raw["id"]
        if started is not None:
            order.created_at = started
        db.add(order)
        orders.append((raw, order))
    await db.flush()
    report.counts["orders created"] = len(orders)

    documents_of: dict[int, list[tuple[dict[str, Any], OrderDocument]]] = {}
    for raw, order in orders:
        rows: list[tuple[dict[str, Any], OrderDocument]] = []
        for d in raw.get("documents") or []:
            dtype = type_by_legacy.get(d["document_type_id"] or 0)
            if (d.get("source_language") or "").lower() not in languages or (
                d.get("target_language") or ""
            ).lower() not in languages:
                report.warn(
                    f"Order {raw['id']}: language "
                    f"{d.get('source_language')}>{d.get('target_language')} unknown."
                )
            document = OrderDocument(
                order_id=order.id,
                document_type_id=dtype.id if dtype else None,
                service_type=ServiceType.WRITTEN,
                source_language=(d.get("source_language") or "ka").lower()[:5],
                target_language=(d.get("target_language") or "ka").lower()[:5],
                page_count=int(d.get("page_count") or 1),
                copy_type=CopyType(d["copy_type"])
                if d.get("copy_type") in {c.value for c in CopyType}
                else CopyType.PLAIN,
                is_notarized=bool(d.get("notarized")),
                price=_money(d.get("price")),
                translator_cost=_money(d.get("translator_cost")),
                notary_cost=_money(d.get("notary_cost")),
                translator_id=translator_of[d["translator_id"]].id
                if d.get("translator_id") in translator_of
                else None,
                notary_id=notary_of[d["notary_id"]].id if d.get("notary_id") in notary_of else None,
            )
            db.add(document)
            rows.append((d, document))
            report.add("order documents")
        documents_of[raw["id"]] = rows
    await db.flush()

    # Status history, oldest first; the order's current status must be last.
    for raw, order in orders:
        events = sorted(raw.get("history") or [], key=lambda h: h.get("at") or "")
        last = None
        for ev in events:
            status = ev["status"]
            if status not in known_statuses:
                report.warn(f"Order {raw['id']}: status '{status}' is not one of Office's.")
            when = moment(ev.get("at")) or datetime.combine(
                order.order_date, datetime.min.time(), tzinfo=UTC
            )
            db.add(
                OrderStatusEvent(
                    order_id=order.id,
                    status=status[:60],
                    changed_at=when,
                    note=_clip(
                        f"Imported. Changed by {ev['user']}" if ev.get("user") else "Imported", 500
                    ),
                )
            )
            last = status
        if not events:
            db.add(
                OrderStatusEvent(
                    order_id=order.id,
                    status=INITIAL_STATUS,
                    changed_at=datetime.combine(order.order_date, datetime.min.time(), tzinfo=UTC),
                    note="Imported",
                )
            )
            last = INITIAL_STATUS
        if raw.get("status") and raw["status"] != last:
            report.warn(
                f"Order {raw['id']}: current status '{raw['status']}' differs from "
                f"history end '{last}'; added."
            )
            db.add(
                OrderStatusEvent(
                    order_id=order.id,
                    status=raw["status"][:60],
                    changed_at=datetime.now(UTC),
                    note="Imported (current status)",
                )
            )
        report.add("status events", max(len(events), 1))

    # Internal notes.
    for raw, order in orders:
        for c in raw.get("comments") or []:
            comment = OrderComment(
                order_id=order.id,
                author_name=(c.get("author_name") or "Unknown")[:255],
                body=c["message"],
                is_pinned=bool(c.get("pinned")),
            )
            posted = moment(c.get("created_at"))
            if posted is not None:
                comment.created_at = posted
            db.add(comment)
            report.add("comments")
        if raw.get("comments") is None:
            report.add("orders with comments skipped (unread in the old system)")

    # ── Money in: client payments ───────────────────────────────────────────
    async def _money_in() -> None:
        groups: dict[Any, dict[str, Any]] = {}
        for raw, order in orders:
            for i, p in enumerate(raw.get("payments") or []):
                if not p.get("amount"):
                    continue
                key = (
                    ("p", p["payment_id"], order.client_id)
                    if p.get("payment_id")
                    else ("row", raw["id"], i)
                )
                g = groups.setdefault(key, {"client": order.client_id, "rows": [], "p": p})
                g["rows"].append((order, _money(p["amount"])))
        payments: list[tuple[ClientPayment, list[tuple[Order, Decimal]]]] = []
        for g in groups.values():
            total = sum((amount for _, amount in g["rows"]), Decimal("0"))
            if total <= 0:
                continue
            p = g["p"]
            payment = ClientPayment(
                client_id=g["client"],
                amount=total,
                payment_date=_day(p.get("date")) or _today(),
                method=payment_method(p.get("method")),
                notes=_clip(f"Imported from Tarjimnebi. {p.get('method') or ''}".strip(), 255),
            )
            db.add(payment)
            payments.append((payment, g["rows"]))
            report.add("client payments")
        await db.flush()
        for payment, rows in payments:
            per_order: dict[int, Decimal] = {}
            for order, amount in rows:
                per_order[order.id] = per_order.get(order.id, Decimal("0")) + amount
            for order_id, amount in per_order.items():
                db.add(
                    ClientPaymentAllocation(
                        payment_id=payment.id, order_id=order_id, amount_allocated=amount
                    )
                )

    await _money_in()

    # ── Money out: translators ──────────────────────────────────────────────
    async def _money_out_translators() -> None:
        groups: dict[Any, dict[str, Any]] = {}
        for raw, order in orders:
            docs = documents_of[raw["id"]]
            for i, p in enumerate(raw.get("translator_payments") or []):
                if not p.get("amount"):
                    continue
                who = p.get("person_id")
                if who not in translator_of:
                    owners = {d.translator_id for _, d in docs if d.translator_id}
                    if len(owners) == 1:
                        who = next(t for t, row in translator_of.items() if row.id in owners)
                    else:
                        report.warn(
                            f"Order {raw['id']}: translator payment has no clear "
                            f"translator; skipped."
                        )
                        continue
                key = ("p", p["payment_id"], who) if p.get("payment_id") else ("row", raw["id"], i)
                g = groups.setdefault(key, {"who": who, "rows": [], "p": p})
                g["rows"].append((order, _money(p["amount"])))
        pending_tp: list[tuple[TranslatorPayment, list[tuple[Order, Decimal]]]] = []
        for g in groups.values():
            total = sum((a for _, a in g["rows"]), Decimal("0"))
            p = g["p"]
            tp = TranslatorPayment(
                translator_id=translator_of[g["who"]].id,
                amount=total,
                payment_date=_day(p.get("date")) or _today(),
                method=payment_method(p.get("method")),
                notes=_clip(f"Imported from Tarjimnebi. {p.get('method') or ''}".strip(), 255),
            )
            db.add(tp)
            pending_tp.append((tp, g["rows"]))
            report.add("translator payments")
        await db.flush()
        for tp, rows in pending_tp:
            per_order: dict[int, Decimal] = {}
            for order, amount in rows:
                per_order[order.id] = per_order.get(order.id, Decimal("0")) + amount
            for order_id, amount in per_order.items():
                db.add(
                    TranslatorPaymentAllocation(
                        payment_id=tp.id, order_id=order_id, amount_allocated=amount
                    )
                )

    await _money_out_translators()

    # ── Money out: notaries (allocated to documents) ────────────────────────
    async def _money_out_notaries() -> None:
        groups: dict[Any, dict[str, Any]] = {}
        for raw, _ in orders:
            for i, p in enumerate(raw.get("notary_payments") or []):
                if not p.get("amount"):
                    continue
                key = ("p", p["payment_id"]) if p.get("payment_id") else ("row", raw["id"], i)
                g = groups.setdefault(key, {"rows": [], "p": p})
                g["rows"].append((raw, _money(p["amount"]), p.get("person_id")))
        pending_np: list[tuple[NotaryPayment, list[tuple[OrderDocument, Decimal]]]] = []
        for g in groups.values():
            allocations: list[tuple[OrderDocument, Decimal]] = []
            for raw, amount, who in g["rows"]:
                owed = [
                    d
                    for row, d in documents_of[raw["id"]]
                    if d.notary_cost
                    and d.notary_cost > 0
                    and (who is None or row.get("notary_id") == who)
                ]
                if not owed:
                    report.warn(
                        f"Order {raw['id']}: notary payment {amount} matches no document "
                        f"with a notary fee; skipped."
                    )
                    continue
                left = amount
                for d in owed:
                    part = min(left, d.notary_cost)
                    if part > 0:
                        allocations.append((d, part))
                        left -= part
                if left > 0:
                    allocations.append((owed[0], left))
            if not allocations:
                continue
            p = g["p"]
            np_ = NotaryPayment(
                amount=sum((a for _, a in allocations), Decimal("0")),
                payment_date=_day(p.get("date")) or _today(),
                method=payment_method(p.get("method")),
                notes=_clip(f"Imported from Tarjimnebi. {p.get('method') or ''}".strip(), 255),
            )
            db.add(np_)
            pending_np.append((np_, allocations))
            report.add("notary payments")
        await db.flush()
        for np_, allocations in pending_np:
            merged: dict[int, Decimal] = {}
            for d, amount in allocations:
                merged[d.id] = merged.get(d.id, Decimal("0")) + amount
            for document_id, amount in merged.items():
                db.add(
                    NotaryPaymentAllocation(
                        payment_id=np_.id, order_document_id=document_id, amount_allocated=amount
                    )
                )

    await _money_out_notaries()

    # ── Expenses ───────────────────────────────────────────────────────────
    order_by_legacy = {raw["id"]: order for raw, order in orders}
    for e in dump.expenses:
        if not e.get("amount") or e["amount"] <= 0:
            continue
        linked = order_by_legacy.get(e["order_id"]) if e.get("order_id") else None
        if e.get("order_id") and linked is None:
            report.warn(f"Expense '{e.get('description')}': order {e['order_id']} not loaded.")
        db.add(
            Expense(
                order_id=linked.id if linked else None,
                expense_date=_day(e["date"]) or _today(),
                category=_clip(e.get("category"), 50) or "Other",
                description=_clip(e.get("description"), 255) or "Imported",
                amount=_money(e["amount"]),
            )
        )
        report.add("expenses")
    await db.flush()

    report.idmap = {
        "clients": {legacy: c.id for legacy, c in client_of.items()},
        "translators": {legacy: t.id for legacy, t in translator_of.items()},
        "notaries": {legacy: n.id for legacy, n in notary_of.items()},
        "orders": {raw["id"]: o.id for raw, o in orders},
    }
    return report
