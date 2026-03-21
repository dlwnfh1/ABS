from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from billing.models import Invoice


class Command(BaseCommand):
    help = "Generate invoices whose issue date is due."

    def add_arguments(self, parser):
        parser.add_argument(
            "--as-of",
            dest="as_of",
            help="Optional date in YYYY-MM-DD format. Defaults to today.",
        )

    def handle(self, *args, **options):
        as_of = options.get("as_of")
        if as_of:
            try:
                as_of_date = datetime.strptime(as_of, "%Y-%m-%d").date()
            except ValueError as exc:
                raise CommandError("--as-of must be in YYYY-MM-DD format.") from exc
        else:
            as_of_date = None

        created = Invoice.generate_due_invoices(as_of_date=as_of_date)
        self.stdout.write(self.style.SUCCESS(f"Generated {len(created)} invoice(s)."))
        for invoice in created:
            self.stdout.write(f"{invoice.invoice_number} {invoice.customer.account_number} {invoice.period_start:%Y-%m-%d}")
