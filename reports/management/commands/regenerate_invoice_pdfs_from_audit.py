import csv
from pathlib import Path
from urllib.parse import quote

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.text import slugify

from billing.models import Invoice
from billing.pdf_utils import get_invoice_output_base_folder, render_invoice_pdf_bytes
from reports.models import InvoiceGenerationBatch, SavedInvoicePDF


class Command(BaseCommand):
    help = "Regenerate invoice PDFs listed in an audit_invoice_pdf_amounts CSV file."

    def add_arguments(self, parser):
        parser.add_argument("audit_csv", help="Path to the CSV created by audit_invoice_pdf_amounts.")
        parser.add_argument(
            "--output-dir",
            dest="output_dir",
            help="Optional folder for regenerated PDFs. Defaults to invoice output folder/regenerated-YYYY-MM-DD.",
        )
        parser.add_argument(
            "--result",
            dest="result_csv",
            help="Optional result CSV path. Defaults to regeneration_result.csv inside the output folder.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Read and validate the CSV without writing PDF files.",
        )

    def handle(self, *args, **options):
        audit_path = Path(options["audit_csv"])
        if not audit_path.exists():
            raise CommandError(f"Audit CSV not found: {audit_path}")

        invoice_numbers = self._read_invoice_numbers(audit_path)
        if not invoice_numbers:
            raise CommandError("No invoice_number values were found in the audit CSV.")

        output_dir = self._resolve_output_dir(options.get("output_dir"))
        result_path = Path(options["result_csv"]) if options.get("result_csv") else output_dir / "regeneration_result.csv"

        rows = []
        batch = None
        for invoice_number in invoice_numbers:
            invoice = Invoice.objects.select_related("customer").filter(invoice_number=invoice_number).first()
            if not invoice:
                rows.append(self._result_row(invoice_number, "missing", "", "Invoice was not found."))
                continue

            output_path = output_dir / self._build_filename(invoice)
            output_path = self._dedupe_path(output_path)
            if options["dry_run"]:
                rows.append(self._result_row(invoice_number, "dry-run", str(output_path), "PDF was not written."))
                continue

            pdf_bytes = render_invoice_pdf_bytes(invoice)
            if not pdf_bytes:
                rows.append(self._result_row(invoice_number, "failed", str(output_path), "PDF rendering failed."))
                continue

            output_dir.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(pdf_bytes)
            batch = batch or self._create_batch(invoice_numbers)
            self._create_saved_pdf_record(batch, invoice, output_path)
            rows.append(self._result_row(invoice_number, "saved", str(output_path), ""))

        if batch:
            batch.saved_count = SavedInvoicePDF.objects.filter(batch=batch).count()
            batch.customer_count = SavedInvoicePDF.objects.filter(batch=batch).values("account_number").distinct().count()
            batch.save(update_fields=["saved_count", "customer_count"])

        self._write_result(rows, result_path, dry_run=options["dry_run"])
        saved_count = sum(1 for row in rows if row["status"] == "saved")
        dry_run_count = sum(1 for row in rows if row["status"] == "dry-run")
        missing_count = sum(1 for row in rows if row["status"] == "missing")
        failed_count = sum(1 for row in rows if row["status"] == "failed")
        self.stdout.write(str(result_path))
        self.stderr.write(
            self.style.SUCCESS(
                f"Processed {len(rows)} invoice(s): saved={saved_count}, dry_run={dry_run_count}, missing={missing_count}, failed={failed_count}."
            )
        )

    def _read_invoice_numbers(self, audit_path):
        invoice_numbers = []
        seen = set()
        with audit_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "invoice_number" not in reader.fieldnames:
                raise CommandError("Audit CSV must include an invoice_number column.")
            for row in reader:
                invoice_number = (row.get("invoice_number") or "").strip()
                if not invoice_number or invoice_number in seen:
                    continue
                seen.add(invoice_number)
                invoice_numbers.append(invoice_number)
        return invoice_numbers

    def _resolve_output_dir(self, output_dir):
        if output_dir:
            path = Path(output_dir)
        base_folder = get_invoice_output_base_folder()
        if not base_folder:
            raise CommandError("No invoice output folder is configured. Pass --output-dir explicitly.")
        if output_dir:
            self._validate_output_dir(path, base_folder)
            return path
        return base_folder / f"regenerated-{timezone.localdate():%Y-%m-%d}"

    def _validate_output_dir(self, output_dir, base_folder):
        try:
            output_dir.resolve().relative_to(base_folder.resolve())
        except ValueError as exc:
            raise CommandError("Output directory must be inside the configured invoice PDF output folder.") from exc

    def _build_filename(self, invoice):
        customer_slug = slugify(invoice.customer.name) or f"customer-{invoice.customer_id}"
        safe_account_number = quote(invoice.customer.account_number, safe="")
        safe_invoice_number = quote(invoice.invoice_number, safe="")
        generated_date = timezone.localdate().strftime("%Y-%m-%d")
        return f"{generated_date}_{safe_account_number}_{customer_slug}_{safe_invoice_number}_REGENERATED.pdf"

    def _dedupe_path(self, path):
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        counter = 2
        while True:
            candidate = parent / f"{stem}-{counter:02d}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    def _create_batch(self, invoice_numbers):
        return InvoiceGenerationBatch.objects.create(
            created_by="Regenerated from invoice PDF audit",
            customer_count=len(invoice_numbers),
        )

    def _create_saved_pdf_record(self, batch, invoice, output_path):
        base_folder = get_invoice_output_base_folder()
        relative_path = str(output_path.relative_to(base_folder)).replace("\\", "/")
        SavedInvoicePDF.objects.create(
            batch=batch,
            invoice=invoice,
            customer=invoice.customer,
            generated_date=timezone.localdate(),
            account_number=invoice.customer.account_number,
            customer_name=invoice.customer.name,
            invoice_number=invoice.invoice_number,
            marker="CURRENT",
            relative_path=relative_path,
            absolute_path=str(output_path),
        )

    def _result_row(self, invoice_number, status, output_path, message):
        return {
            "invoice_number": invoice_number,
            "status": status,
            "output_path": output_path,
            "message": message,
        }

    def _write_result(self, rows, result_path, dry_run=False):
        fieldnames = ["invoice_number", "status", "output_path", "message"]
        if not dry_run:
            result_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            result_path.parent.mkdir(parents=True, exist_ok=True)
        with result_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
