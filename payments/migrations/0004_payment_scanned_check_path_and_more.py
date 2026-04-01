from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0003_payment_is_voided_payment_void_reason_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="scanned_check_path",
            field=models.CharField(blank=True, max_length=1000),
        ),
        migrations.AddField(
            model_name="payment",
            name="scanned_check_uploaded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
