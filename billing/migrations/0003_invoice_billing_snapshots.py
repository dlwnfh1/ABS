from datetime import timedelta
from decimal import Decimal

from django.db import migrations, models


def add_months(value, months):
    year = value.year + ((value.month - 1 + months) // 12)
    month = ((value.month - 1 + months) % 12) + 1
    month_lengths = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(value.day, month_lengths[month - 1])
    return value.replace(year=year, month=month, day=day)


def derive_billing_term(invoice, customer):
    for months in (3, 6, 9, 12):
        if add_months(invoice.period_start, months) - timedelta(days=1) == invoice.period_end:
            return months
    return customer.billing_term


def backfill_invoice_snapshots(apps, schema_editor):
    Invoice = apps.get_model("billing", "Invoice")
    InvoiceItem = apps.get_model("billing", "InvoiceItem")

    for invoice in Invoice.objects.select_related("customer").all().iterator():
        current_item = (
            InvoiceItem.objects.filter(invoice_id=invoice.id, line_type="current_period")
            .order_by("period_start", "id")
            .first()
        )
        invoice.billing_term_snapshot = derive_billing_term(invoice, invoice.customer)
        invoice.billing_amount_snapshot = (
            Decimal(current_item.amount).quantize(Decimal("0.01"))
            if current_item and current_item.amount is not None
            else Decimal(invoice.subtotal or Decimal("0.00")).quantize(Decimal("0.01"))
        )
        invoice.billing_description_snapshot = (
            (current_item.description or "").strip()
            if current_item and current_item.description
            else "Alarm Monitoring Service"
        )
        invoice.tax_rate_snapshot = Decimal(invoice.tax_rate or Decimal("0.000")).quantize(Decimal("0.001"))
        invoice.save(
            update_fields=[
                "billing_term_snapshot",
                "billing_amount_snapshot",
                "billing_description_snapshot",
                "tax_rate_snapshot",
            ]
        )


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0002_alter_invoice_tax_rate_precision"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="billing_amount_snapshot",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name="invoice",
            name="billing_description_snapshot",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="invoice",
            name="billing_term_snapshot",
            field=models.PositiveSmallIntegerField(
                blank=True,
                choices=[(3, "3 Months"), (6, "6 Months"), (9, "9 Months"), (12, "12 Months")],
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="tax_rate_snapshot",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=6, null=True),
        ),
        migrations.RunPython(backfill_invoice_snapshots, migrations.RunPython.noop),
    ]
