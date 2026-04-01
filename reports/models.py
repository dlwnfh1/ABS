from django.db import models


class ReportCenter(models.Model):
    title = models.CharField(max_length=100, default="Reports")

    class Meta:
        verbose_name = "Reports"
        verbose_name_plural = "Reports"

    def __str__(self):
        return self.title


class SystemSetting(models.Model):
    invoice_pdf_output_folder = models.CharField(
        max_length=500,
        blank=True,
        help_text="Folder where generated invoice PDFs will be saved automatically. Example: D:\BillingPDFs",
    )
    payment_check_scan_folder = models.CharField(
        max_length=500,
        blank=True,
        help_text="Folder where uploaded scanned check files will be saved. Example: D:\CheckScans",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "System Setting"
        verbose_name_plural = "System Settings"

    def __str__(self):
        return "System Settings"

    @classmethod
    def get_solo(cls):
        return cls.objects.order_by("id").first()
