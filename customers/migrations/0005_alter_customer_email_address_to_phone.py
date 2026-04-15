from django.core.validators import RegexValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("customers", "0004_alter_service_service_name"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customer",
            name="email_address",
            field=models.CharField(
                blank=True,
                max_length=12,
                validators=[
                    RegexValidator(
                        regex=r"^\d{3}-\d{3}-\d{4}$",
                        message="Phone number must be entered in 123-456-7890 format.",
                    )
                ],
                verbose_name="Phone number",
            ),
        ),
    ]
