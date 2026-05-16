from decimal import Decimal
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0004_payment_scanned_check_path_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="PaymentSettlement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("mode", models.CharField(choices=[("tax_inclusive", "Tax-Inclusive Full Settlement")], default="tax_inclusive", max_length=30)),
                ("actual_received", models.DecimalField(decimal_places=2, max_digits=10)),
                ("recognized_subtotal", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
                ("recognized_tax", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
                ("adjustment_subtotal", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
                ("adjustment_tax", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
                ("note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("customer", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payment_settlements", to="customers.customer")),
                ("payment", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="settlement", to="payments.payment")),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="PaymentSettlementAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="settlement_allocations", to="billing.invoice")),
                ("settlement", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="allocations", to="payments.paymentsettlement")),
            ],
            options={
                "ordering": ["invoice__period_start", "id"],
                "constraints": [
                    models.UniqueConstraint(fields=("settlement", "invoice"), name="unique_invoice_settlement_allocation_per_payment"),
                ],
            },
        ),
    ]
