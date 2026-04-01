from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reports", "0002_systemsetting"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemsetting",
            name="payment_check_scan_folder",
            field=models.CharField(
                blank=True,
                help_text="Folder where uploaded scanned check files will be saved. Example: D:\\CheckScans",
                max_length=500,
            ),
        ),
    ]
