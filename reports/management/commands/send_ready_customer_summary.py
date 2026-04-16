from django.core.management.base import BaseCommand
from django.utils import timezone

from billing.models import Invoice
from reports.notifications import send_ready_customer_summary


class Command(BaseCommand):
    help = "Send the Ready customer count email to admin recipients."

    def handle(self, *args, **options):
        today = timezone.localdate()
        ready_count = sum(
            1
            for candidate in Invoice.get_generation_candidates(as_of_date=today)
            if candidate["status"] == "ready"
        )

        if ready_count <= 0:
            self.stdout.write("No ready customers. No email sent.")
            return

        sent_count = send_ready_customer_summary(ready_count)
        if sent_count:
            self.stdout.write(f"Sent ready customer summary for {ready_count} customers.")
        else:
            self.stdout.write("Ready customers found, but no admin recipients are configured.")
