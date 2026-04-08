from django.db import models


class ReportCenter(models.Model):
    title = models.CharField(max_length=100, default="Reports")

    class Meta:
        verbose_name = "Reports"
        verbose_name_plural = "Reports"

    def __str__(self):
        return self.title


class DispatchCenter(models.Model):
    title = models.CharField(max_length=100, default="Invoice Dispatch")

    class Meta:
        verbose_name = "Invoice Dispatch"
        verbose_name_plural = "Invoice Dispatch"

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


class InvoiceGenerationBatch(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=150, blank=True)
    saved_count = models.PositiveIntegerField(default=0)
    customer_count = models.PositiveIntegerField(default=0)
    is_printed = models.BooleanField(default=False)
    printed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Invoice generation batch"
        verbose_name_plural = "Invoice generation batches"

    def __str__(self):
        return self.label

    @property
    def label(self):
        return f"BATCH-{self.created_at:%Y%m%d-%H%M%S}"


class SavedInvoicePDF(models.Model):
    MARKER_CHOICES = (
        ("CURRENT", "Current"),
        ("PRIOR", "Prior"),
    )

    batch = models.ForeignKey(InvoiceGenerationBatch, on_delete=models.CASCADE, related_name="saved_invoices")
    invoice = models.ForeignKey("billing.Invoice", on_delete=models.SET_NULL, null=True, blank=True, related_name="saved_pdf_records")
    customer = models.ForeignKey("customers.Customer", on_delete=models.SET_NULL, null=True, blank=True, related_name="saved_invoice_pdfs")
    generated_date = models.DateField()
    account_number = models.CharField(max_length=50)
    customer_name = models.CharField(max_length=255)
    invoice_number = models.CharField(max_length=100)
    marker = models.CharField(max_length=10, choices=MARKER_CHOICES)
    relative_path = models.CharField(max_length=500)
    absolute_path = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Saved invoice PDF"
        verbose_name_plural = "Saved invoice PDFs"

    def __str__(self):
        return f"{self.invoice_number} ({self.marker})"
