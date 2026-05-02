from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("customers", "0008_alter_customer_tax_rate_precision"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="invoice_email",
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name="customer",
            name="invoice_delivery_method",
            field=models.CharField(
                choices=[
                    ("mail", "Mail"),
                    ("email", "Email"),
                    ("both", "Mail + Email"),
                    ("none", "Do Not Send"),
                ],
                default="mail",
                max_length=10,
            ),
        ),
    ]
