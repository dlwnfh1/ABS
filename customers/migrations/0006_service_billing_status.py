from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("customers", "0005_alter_customer_email_address_to_phone"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="billing_status",
            field=models.CharField(
                choices=[
                    ("billable", "Billable"),
                    ("hold", "Billing Hold"),
                    ("complimentary", "Complimentary"),
                    ("inactive", "Inactive"),
                ],
                default="billable",
                max_length=20,
            ),
        ),
    ]
