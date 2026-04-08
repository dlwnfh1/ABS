from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reports", "0004_invoicegenerationbatch_savedinvoicepdf"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoicegenerationbatch",
            name="is_printed",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="invoicegenerationbatch",
            name="printed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
