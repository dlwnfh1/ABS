import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from billing.models import Invoice
from reports.models import SavedInvoicePDF


class Command(BaseCommand):
    help = "Audit saved CURRENT invoice PDFs that may show an understated Amount Due."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date-from",
            dest="date_from",
            help="Only include saved PDFs generated on or after this YYYY-MM-DD date.",
        )
        parser.add_argument(
            "--date-to",
            dest="date_to",
            help="Only include saved PDFs generated on or before this YYYY-MM-DD date.",
        )
        parser.add_argument(
            "--output",
            dest="output",
            help="Optional CSV output path. Defaults to printing CSV to stdout.",
        )

    def handle(self, *args, **options):
        date_from = self._parse_date(options.get("date_from"), "--date-from")
        date_to = self._parse_date(options.get("date_to"), "--date-to")

        records = SavedInvoicePDF.objects.select_related("invoice", "customer").filter(
            marker="CURRENT",
            invoice__isnull=False,
        )
        if date_from:
            records = records.filter(generated_date__gte=date_from)
        if date_to:
            records = records.filter(generated_date__lte=date_to)

        rows = []
        seen = set()
        for saved_pdf in records.order_by("generated_date", "account_number", "invoice_number", "id"):
            key = (saved_pdf.generated_date, saved_pdf.account_number, saved_pdf.invoice_number, saved_pdf.absolute_path)
            if key in seen:
                continue
            seen.add(key)

            invoice = saved_pdf.invoice
            latest_issued_invoice = (
                invoice.customer.invoices.exclude(status=Invoice.STATUS_VOID)
                .filter(issue_date__lte=saved_pdf.generated_date)
                .order_by("-period_start", "-id")
                .first()
            )
            used_open_amount_path = not latest_issued_invoice or latest_issued_invoice.pk != invoice.pk
            if not used_open_amount_path:
                continue

            line_items = list(invoice.items.order_by("period_start", "id"))
            carryover_count = sum(1 for item in line_items if item.line_type == "carryover")
            displayed_amount_due = invoice.amount_due_for_allocation(saved_pdf.generated_date)
            invoice_display_total = (Decimal(invoice.subtotal) + Decimal(invoice.tax_amount)).quantize(Decimal("0.01"))
            difference = (invoice_display_total - displayed_amount_due).quantize(Decimal("0.01"))

            if difference <= Decimal("0.00"):
                continue

            rows.append(
                {
                    "generated_date": saved_pdf.generated_date.isoformat(),
                    "issue_date": invoice.issue_date.isoformat() if invoice.issue_date else "",
                    "due_date": invoice.due_date.isoformat() if invoice.due_date else "",
                    "account_number": saved_pdf.account_number,
                    "customer_name": saved_pdf.customer_name,
                    "invoice_number": saved_pdf.invoice_number,
                    "line_item_count": len(line_items),
                    "carryover_count": carryover_count,
                    "invoice_display_total": f"{invoice_display_total:.2f}",
                    "amount_due_likely_shown": f"{displayed_amount_due:.2f}",
                    "difference": f"{difference:.2f}",
                    "absolute_path": saved_pdf.absolute_path,
                }
            )

        self._write_rows(rows, options.get("output"))
        self.stderr.write(self.style.SUCCESS(f"Found {len(rows)} possible affected saved CURRENT PDF(s)."))

    def _parse_date(self, value, option_name):
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise CommandError(f"{option_name} must be in YYYY-MM-DD format.") from exc

    def _write_rows(self, rows, output_path):
        fieldnames = [
            "generated_date",
            "issue_date",
            "due_date",
            "account_number",
            "customer_name",
            "invoice_number",
            "line_item_count",
            "carryover_count",
            "invoice_display_total",
            "amount_due_likely_shown",
            "difference",
            "absolute_path",
        ]
        if output_path:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            self.stdout.write(str(path))
            return

        writer = csv.DictWriter(self.stdout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
