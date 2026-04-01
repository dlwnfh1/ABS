import base64
import hashlib
from datetime import timedelta
from io import BytesIO
from pathlib import Path

from django.utils import timezone
from django.utils.text import slugify
from xhtml2pdf import pisa

from reports.models import SystemSetting

from .models import Invoice


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


def save_invoices_to_configured_folder(invoices):
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

    saved_files = []
    for invoice in invoices:
        pdf_bytes = render_invoice_pdf_bytes(invoice)
        if not pdf_bytes:
            continue
        marker = "CURRENT" if latest_by_customer.get(invoice.customer_id) and latest_by_customer[invoice.customer_id].pk == invoice.pk else "PRIOR"
        customer_slug = slugify(invoice.customer.name) or f"customer-{invoice.customer_id}"
        filename = f"{generation_date.strftime('%Y-%m-%d')}_{invoice.customer.account_number}_{customer_slug}_{invoice.invoice_number}_{marker}.pdf"
        output_path = date_folder / filename
        output_path.write_bytes(pdf_bytes)
        saved_files.append(output_path)

    return {
        "base_folder": str(base_folder),
        "date_folder": str(date_folder),
        "saved_count": len(saved_files),
        "saved_files": [str(path) for path in saved_files],
    }
