from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("customers", "0009_customer_invoice_delivery_fields"),
    ]

    operations = [
        migrations.RenameField(
            model_name="customer",
            old_name="invoice_email",
            new_name="invoice_email_to",
        ),
        migrations.AddField(
            model_name="customer",
            name="invoice_email_cc",
            field=models.TextField(blank=True),
        ),
        migrations.AlterField(
            model_name="customer",
            name="invoice_email_to",
            field=models.TextField(blank=True),
        ),
    ]
