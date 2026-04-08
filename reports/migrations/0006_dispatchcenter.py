from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reports", "0005_invoicegenerationbatch_printed_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="DispatchCenter",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(default="Invoice Dispatch", max_length=100)),
            ],
            options={
                "verbose_name": "Invoice Dispatch",
                "verbose_name_plural": "Invoice Dispatch",
            },
        ),
    ]
