from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum


class Customer(models.Model):
    BILLING_TERM_CHOICES = (
        (3, "3 Months"),
        (6, "6 Months"),
        (9, "9 Months"),
        (12, "12 Months"),
    )

    name = models.CharField(max_length=255)
    account_number = models.CharField(max_length=50, unique=True)
    billing_address1 = models.CharField(max_length=255)
    billing_address2 = models.CharField(max_length=255, blank=True)
    email_address = models.EmailField(blank=True)
    billing_term = models.PositiveSmallIntegerField(choices=BILLING_TERM_CHOICES, default=3)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    first_billing_date = models.DateField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "account_number"]

    def __str__(self) -> str:
        return f"{self.name} ({self.account_number})"

    def clean(self):
        if self.first_billing_date and self.billing_term not in dict(self.BILLING_TERM_CHOICES):
            raise ValidationError("Billing term must be one of 3, 6, 9, or 12 months.")

    @property
    def current_billing_amount(self) -> Decimal:
        amount = self.services.filter(is_active=True).aggregate(total=Sum("billing_amount"))["total"] or Decimal("0.00")
        return Decimal(amount).quantize(Decimal("0.01"))

    def can_generate_initial_invoice(self) -> bool:
        return bool(self.first_billing_date and self.is_active and self.services.filter(is_active=True).exists())

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.ensure_initial_invoice()

    def ensure_initial_invoice(self):
        if not self.can_generate_initial_invoice():
            return None

        from billing.models import Invoice, add_months

        period_start = self.first_billing_date
        period_end = add_months(period_start, self.billing_term) - timedelta(days=1)
        existing = Invoice.objects.filter(customer=self, period_start=period_start, period_end=period_end).first()
        if existing:
            return existing

        invoice = Invoice(
            customer=self,
            period_start=period_start,
            period_end=period_end,
            issue_date=period_start - timedelta(days=15),
            due_date=period_start,
            auto_generated=False,
            status=Invoice.STATUS_ISSUED,
        )
        invoice.save(create_followup=False)
        return invoice


class Service(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="services")
    service_name = models.CharField(max_length=255, default="Primary Service")
    service_address1 = models.CharField(max_length=255)
    service_address2 = models.CharField(max_length=255, blank=True)
    activation_date = models.DateField(blank=True, null=True)
    billing_amount = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["customer__name", "service_name", "id"]

    def __str__(self) -> str:
        return f"{self.customer.account_number} - {self.service_name}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.customer.ensure_initial_invoice()
