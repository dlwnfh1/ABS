from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("customers", "0007_customer_auto_ach"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customer",
            name="tax_rate",
            field=models.DecimalField(decimal_places=3, default=Decimal("0.000"), max_digits=6),
        ),
    ]
