import base64
import hashlib
import re
from datetime import datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, unquote

from django.utils import timezone
from django.utils.text import slugify
from pypdf import PdfWriter
from xhtml2pdf import pisa

from customers.models import Customer
from reports.models import InvoiceGenerationBatch, SavedInvoicePDF, SystemSetting

from .models import Invoice


SAVED_INVOICE_FILENAME_RE = re.compile(
    r"^(?P<generated>\d{4}-\d{2}-\d{2})_"
    r"(?P<account>[^_]+)_"
    r"(?P<customer>[^_]+)_"
    r"(?P<invoice>INV-[^_]+)_"
    r"(?P<marker>CURRENT|PRIOR)\.pdf$",
    re.IGNORECASE,
)


def ensure_md5_compat():
    original_md5 = hashlib.md5

    def md5_compat(*args, **kwargs):
        kwargs.pop("usedforsecurity", None)
        if len(args) > 1:
            args = args[:1]
        return original_md5(*args, **kwargs)

    hashlib.md5 = md5_compat

    module_patches = [
        ("reportlab.pdfbase.pdfdoc", "md5"),
        ("reportlab.pdfbase.cidfonts", "md5"),
        ("reportlab.lib.utils", "md5"),
        ("reportlab.lib.fontfinder", "md5"),
    ]
    for module_name, attr_name in module_patches:
        try:
            module = __import__(module_name, fromlist=[attr_name])
            setattr(module, attr_name, md5_compat)
        except Exception:
            pass


def logo_symbol_data_uri():
    logo_path = Path(__file__).resolve().parent.parent / "logo_candidate.png"
    if not logo_path.exists():
        return ""
    encoded = base64.b64encode(logo_path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def portal_logo_data_uri():
    portal_logo_path = Path(__file__).resolve().parent.parent / "portal_logo.png"
    if portal_logo_path.exists():
        encoded = base64.b64encode(portal_logo_path.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    return logo_symbol_data_uri()


def build_invoice_pdf_context(invoice):
    today = timezone.localdate()
    latest_issued_invoice = (
        invoice.customer.invoices.exclude(status=Invoice.STATUS_VOID)
        .filter(issue_date__lte=today)
        .order_by("-period_start", "-id")
        .first()
    )
    source_items = list(invoice.items.order_by("period_start", "id"))
    items = list(reversed(source_items))
    padded_items = [
        {
            "description": item.description,
            "period_start": item.period_start,
            "period_end": item.period_end,
            "amount": item.amount,
            "line_type": item.line_type,
        }
        for item in items
    ]
    if len(padded_items) > 5:
        overflow_items = padded_items[4:]
        padded_items = padded_items[:4]
        padded_items.append(
            {
                "description": "Additional prior billing periods",
                "period_start": overflow_items[-1]["period_start"],
                "period_end": overflow_items[0]["period_end"],
                "amount": sum(item["amount"] for item in overflow_items),
                "line_type": "rollup",
            }
        )
    while len(padded_items) < 5:
        padded_items.append(
            {
                "description": "",
                "period_start": None,
                "period_end": None,
                "amount": None,
                "line_type": "",
            }
        )

    billing_to = invoice.customer.billing_address1
    if invoice.customer.billing_address2:
        billing_to = f"{billing_to}, {invoice.customer.billing_address2}"

    primary_service = invoice.customer.services.filter(is_active=True).order_by("id").first()
    if primary_service:
        service_at = primary_service.service_address1
        if primary_service.service_address2:
            service_at = f"{service_at}, {primary_service.service_address2}"
    else:
        service_at = billing_to

    return {
        "invoice": invoice,
        "items": items,
        "padded_items": padded_items,
        "current_balance_due": invoice.customer.open_balance_as_of(today) if latest_issued_invoice and latest_issued_invoice.pk == invoice.pk else invoice.amount_due_for_allocation(today),
        "preview_date": today,
        "is_latest_issued_invoice": bool(latest_issued_invoice and latest_issued_invoice.pk == invoice.pk),
        "logo_symbol_data_uri": logo_symbol_data_uri(),
        "billing_to_display": billing_to,
        "service_at_display": service_at,
    }


def render_invoice_pdf_bytes(invoice):
    from django.template.loader import render_to_string

    html = render_to_string("admin/billing/invoice/pdf.html", build_invoice_pdf_context(invoice))
    pdf_buffer = BytesIO()
    ensure_md5_compat()
    pdf = pisa.CreatePDF(html, dest=pdf_buffer)
    if pdf.err:
        return None
    return pdf_buffer.getvalue()


def get_invoice_output_base_folder():
    settings_obj = SystemSetting.get_solo()
    if not settings_obj or not settings_obj.invoice_pdf_output_folder:
        return None
    return Path(settings_obj.invoice_pdf_output_folder)


def parse_saved_invoice_pdf_record(path, base_folder=None):
    match = SAVED_INVOICE_FILENAME_RE.match(path.name)
    if not match:
        return None
    relative_path = path.relative_to(base_folder) if base_folder else path
    customer_slug = match.group("customer")
    return {
        "path": path,
        "relative_path": str(relative_path).replace("\\", "/"),
        "filename": path.name,
        "generated_date": match.group("generated"),
        "account_number": match.group("account"),
        "customer_label": customer_slug.replace("-", " ").title(),
        "invoice_number": match.group("invoice"),
        "marker": match.group("marker").upper(),
    }


def sync_saved_invoice_pdf_records_from_disk():
    base_folder = get_invoice_output_base_folder()
    if not base_folder or not base_folder.exists():
        return 0

    existing_paths = set(SavedInvoicePDF.objects.values_list("absolute_path", flat=True))
    imported_count = 0
    imported_batches = {}

    for path in sorted(base_folder.rglob("*.pdf")):
        if str(path) in existing_paths:
            continue
        parsed = parse_saved_invoice_pdf_record(path, base_folder=base_folder)
        if not parsed:
            continue

        generated_date = datetime.strptime(parsed["generated_date"], "%Y-%m-%d").date()
        batch = imported_batches.get(generated_date)
        if batch is None:
            batch = (
                InvoiceGenerationBatch.objects.filter(
                    created_by="Imported from saved folder",
                    created_at__date=generated_date,
                )
                .order_by("id")
                .first()
            )
            if batch is None:
                batch = InvoiceGenerationBatch.objects.create(
                    created_by="Imported from saved folder",
                    saved_count=0,
                    customer_count=0,
                )
                batch.created_at = timezone.make_aware(datetime.combine(generated_date, time(12, 0)))
                batch.save(update_fields=["created_at"])
            imported_batches[generated_date] = batch

        invoice = Invoice.objects.filter(invoice_number=parsed["invoice_number"]).select_related("customer").first()
        customer = None
        customer_name = parsed["customer_label"]
        account_number = parsed["account_number"]
        if invoice:
            customer = invoice.customer
            customer_name = customer.name
            account_number = customer.account_number
        else:
            customer = Customer.objects.filter(account_number=account_number).first()
            if customer:
                customer_name = customer.name

        SavedInvoicePDF.objects.create(
            batch=batch,
            invoice=invoice,
            customer=customer,
            generated_date=generated_date,
            account_number=account_number,
            customer_name=customer_name,
            invoice_number=parsed["invoice_number"],
            marker=parsed["marker"],
            relative_path=parsed["relative_path"],
            absolute_path=str(path),
        )
        imported_count += 1

    for batch in imported_batches.values():
        batch.saved_count = batch.saved_invoices.count()
        batch.customer_count = batch.saved_invoices.values("account_number").distinct().count()
        batch.save(update_fields=["saved_count", "customer_count"])

    return imported_count


def list_saved_invoice_pdf_records(query="", account_number=None, latest_only=False, limit=300, date_from=None, date_to=None, marker=None, batch_id=None, printed_scope=None):
    sync_saved_invoice_pdf_records_from_disk()
    base_folder = get_invoice_output_base_folder()
    records_qs = SavedInvoicePDF.objects.select_related("batch", "customer", "invoice").all()

    lowered_query = (query or "").strip().lower()
    target_account = (account_number or "").strip().lower()
    if target_account:
        records_qs = records_qs.filter(account_number__iexact=target_account)
    if date_from:
        records_qs = records_qs.filter(generated_date__gte=date_from)
    if date_to:
        records_qs = records_qs.filter(generated_date__lte=date_to)
    if marker:
        records_qs = records_qs.filter(marker=marker.upper())
    if batch_id:
        records_qs = records_qs.filter(batch_id=batch_id)
    elif printed_scope == "unprinted":
        records_qs = records_qs.filter(batch__is_printed=False)
    elif printed_scope == "printed":
        records_qs = records_qs.filter(batch__is_printed=True)

    records = []
    for saved in records_qs.order_by("-generated_date", "-id"):
        record = {
            "path": Path(saved.absolute_path),
            "relative_path": saved.relative_path.replace("\\", "/"),
            "filename": Path(saved.absolute_path).name,
            "generated_date": saved.generated_date.strftime("%Y-%m-%d"),
            "account_number": saved.account_number,
            "customer_label": saved.customer_name,
            "invoice_number": saved.invoice_number,
            "marker": saved.marker.upper(),
            "batch_id": saved.batch_id,
            "batch_label": saved.batch.label if saved.batch_id else "",
            "batch_created_at": saved.batch.created_at if saved.batch_id else None,
            "batch_is_printed": bool(saved.batch_id and saved.batch.is_printed),
            "batch_printed_at": saved.batch.printed_at if saved.batch_id else None,
        }
        haystack = " ".join(
            [
                record["filename"] or "",
                record["account_number"],
                record["customer_label"],
                record["invoice_number"],
                record["generated_date"],
                record["batch_label"],
            ]
        ).lower()
        if lowered_query and lowered_query not in haystack:
            continue
        records.append(record)

    if latest_only:
        latest_records = {}
        for record in records:
            existing = latest_records.get(record["account_number"])
            if not existing:
                latest_records[record["account_number"]] = record
                continue
            if record["marker"] == "CURRENT" and existing["marker"] != "CURRENT":
                latest_records[record["account_number"]] = record
                continue
            if record["generated_date"] > existing["generated_date"]:
                latest_records[record["account_number"]] = record
        records = list(latest_records.values())
        records.sort(
            key=lambda item: (item["generated_date"], item["account_number"], item["filename"]),
            reverse=True,
        )

    if limit:
        records = records[:limit]
    recent_batches = list(InvoiceGenerationBatch.objects.order_by("-created_at", "-id")[:20])
    latest_batch = recent_batches[0] if recent_batches else None
    return {"base_folder": base_folder, "records": records, "recent_batches": recent_batches, "latest_batch": latest_batch}


def merge_saved_invoice_pdf_records(records):
    writer = PdfWriter()
    added = 0
    for record in records:
        path = record.get("path")
        if not path or not path.exists():
            continue
        writer.append(str(path))
        added += 1
    if not added:
        return b""
    output = BytesIO()
    writer.write(output)
    writer.close()
    return output.getvalue()


def save_invoices_to_configured_folder(invoices, created_by=""):
    invoices = [invoice for invoice in invoices if invoice is not None]
    settings_obj = SystemSetting.get_solo()
    if not settings_obj or not settings_obj.invoice_pdf_output_folder:
        return None

    base_folder = Path(settings_obj.invoice_pdf_output_folder)
    generation_date = timezone.localdate()
    date_folder = base_folder / generation_date.strftime("%Y-%m-%d")
    date_folder.mkdir(parents=True, exist_ok=True)

    latest_by_customer = {}
    for invoice in invoices:
        current = latest_by_customer.get(invoice.customer_id)
        if not current or (invoice.period_start, invoice.id) > (current.period_start, current.id):
            latest_by_customer[invoice.customer_id] = invoice

    batch = InvoiceGenerationBatch.objects.create(
        created_by=(created_by or "")[:150],
        customer_count=len(latest_by_customer),
    )
    saved_files = []
    for invoice in invoices:
        pdf_bytes = render_invoice_pdf_bytes(invoice)
        if not pdf_bytes:
            continue
        marker = "CURRENT" if latest_by_customer.get(invoice.customer_id) and latest_by_customer[invoice.customer_id].pk == invoice.pk else "PRIOR"
        customer_slug = slugify(invoice.customer.name) or f"customer-{invoice.customer_id}"
        safe_account_number = quote(invoice.customer.account_number, safe="")
        safe_invoice_number = quote(invoice.invoice_number, safe="")
        filename = f"{generation_date.strftime('%Y-%m-%d')}_{safe_account_number}_{customer_slug}_{safe_invoice_number}_{marker}.pdf"
        output_path = date_folder / filename
        output_path.write_bytes(pdf_bytes)
        saved_files.append(output_path)
        SavedInvoicePDF.objects.create(
            batch=batch,
            invoice=invoice,
            customer=invoice.customer,
            generated_date=generation_date,
            account_number=invoice.customer.account_number,
            customer_name=invoice.customer.name,
            invoice_number=invoice.invoice_number,
            marker=marker,
            relative_path=str(output_path.relative_to(base_folder)).replace("\\", "/"),
            absolute_path=str(output_path),
        )

    batch.saved_count = len(saved_files)
    batch.save(update_fields=["saved_count"])

    return {
        "base_folder": str(base_folder),
        "date_folder": str(date_folder),
        "saved_count": len(saved_files),
        "saved_files": [str(path) for path in saved_files],
        "batch_id": batch.pk,
        "batch_label": batch.label,
    }
