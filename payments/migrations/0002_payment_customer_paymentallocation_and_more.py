from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def migrate_existing_payments(apps, schema_editor):
    Payment = apps.get_model("payments", "Payment")
    PaymentAllocation = apps.get_model("payments", "PaymentAllocation")

    for payment in Payment.objects.select_related("invoice__customer"):
        payment.customer_id = payment.invoice.customer_id
        payment.save(update_fields=["customer"])
        PaymentAllocation.objects.create(
            payment_id=payment.id,
            invoice_id=payment.invoice_id,
            amount=payment.amount,
        )


def reverse_existing_payments(apps, schema_editor):
    Payment = apps.get_model("payments", "Payment")
    PaymentAllocation = apps.get_model("payments", "PaymentAllocation")

    for payment in Payment.objects.all():
        allocation = PaymentAllocation.objects.filter(payment_id=payment.id).order_by("id").first()
        if allocation:
            payment.invoice_id = allocation.invoice_id
            payment.save(update_fields=["invoice"])


class Migration(migrations.Migration):

    dependencies = [
        ("customers", "0003_alter_service_activation_date"),
        ("payments", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="customer",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="payments",
                to="customers.customer",
            ),
        ),
        migrations.CreateModel(
            name="PaymentAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="allocations", to="billing.invoice")),
                ("payment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="allocations", to="payments.payment")),
            ],
            options={
                "ordering": ["invoice__period_start", "id"],
            },
        ),
        migrations.RunPython(migrate_existing_payments, reverse_existing_payments),
        migrations.RemoveField(
            model_name="payment",
            name="invoice",
        ),
        migrations.AlterField(
            model_name="payment",
            name="customer",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payments", to="customers.customer"),
        ),
        migrations.AddConstraint(
            model_name="paymentallocation",
            constraint=models.UniqueConstraint(fields=("payment", "invoice"), name="unique_invoice_allocation_per_payment"),
        ),
    ]
