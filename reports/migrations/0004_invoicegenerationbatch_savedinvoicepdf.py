from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0001_initial"),
        ("customers", "0004_alter_service_service_name"),
        ("reports", "0003_systemsetting_payment_check_scan_folder"),
    ]

    operations = [
        migrations.CreateModel(
            name="InvoiceGenerationBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.CharField(blank=True, max_length=150)),
                ("saved_count", models.PositiveIntegerField(default=0)),
                ("customer_count", models.PositiveIntegerField(default=0)),
            ],
            options={
                "verbose_name": "Invoice generation batch",
                "verbose_name_plural": "Invoice generation batches",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="SavedInvoicePDF",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("generated_date", models.DateField()),
                ("account_number", models.CharField(max_length=50)),
                ("customer_name", models.CharField(max_length=255)),
                ("invoice_number", models.CharField(max_length=100)),
                ("marker", models.CharField(choices=[("CURRENT", "Current"), ("PRIOR", "Prior")], max_length=10)),
                ("relative_path", models.CharField(max_length=500)),
                ("absolute_path", models.CharField(max_length=1000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="saved_invoices", to="reports.invoicegenerationbatch")),
                ("customer", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="saved_invoice_pdfs", to="customers.customer")),
                ("invoice", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="saved_pdf_records", to="billing.invoice")),
            ],
            options={
                "verbose_name": "Saved invoice PDF",
                "verbose_name_plural": "Saved invoice PDFs",
                "ordering": ["-created_at", "-id"],
            },
        ),
    ]
