from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="invoice",
            name="tax_rate",
            field=models.DecimalField(decimal_places=3, default=Decimal("0.000"), max_digits=6),
        ),
    ]
