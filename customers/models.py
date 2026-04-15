from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone


PHONE_NUMBER_VALIDATOR = RegexValidator(
    regex=r"^\d{3}-\d{3}-\d{4}$",
    message="Phone number must be entered in 123-456-7890 format.",
)


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
    email_address = models.CharField(
        "Phone number",
        max_length=12,
        blank=True,
        validators=[PHONE_NUMBER_VALIDATOR],
    )
    billing_term = models.PositiveSmallIntegerField(choices=BILLING_TERM_CHOICES, default=3)
    auto_ach = models.BooleanField(default=False)
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
    def billable_services(self):
        return self.services.filter(is_active=True, billing_status=Service.BILLING_STATUS_BILLABLE)

    @property
    def current_billing_amount(self) -> Decimal:
        amount = self.billable_services.aggregate(total=Sum("billing_amount"))["total"] or Decimal("0.00")
        return Decimal(amount).quantize(Decimal("0.01"))

    def can_generate_initial_invoice(self) -> bool:
        return bool(self.first_billing_date and self.is_active and self.billable_services.exists())

    def open_balance_as_of(self, as_of_date=None) -> Decimal:
        from billing.models import Invoice

        as_of_date = as_of_date or timezone.localdate()
        latest_issued_invoice = (
            self.invoices.exclude(status=Invoice.STATUS_VOID)
            .filter(issue_date__lte=as_of_date)
            .order_by("-period_start", "-id")
            .first()
        )
        if not latest_issued_invoice:
            return Decimal("0.00")

        gross_total = latest_issued_invoice.statement_base_totals()["gross_total"]
        payments_after_issue = (
            self.payments.filter(
                is_voided=False,
                payment_date__gt=latest_issued_invoice.issue_date,
                payment_date__lte=as_of_date,
            )
            .aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        )
        balance = Decimal(gross_total) - Decimal(payments_after_issue)
        if balance < Decimal("0.00"):
            balance = Decimal("0.00")
        return balance.quantize(Decimal("0.01"))

    def next_expected_issue_date(self):
        from billing.models import Invoice

        latest_invoice = self.invoices.exclude(status=Invoice.STATUS_VOID).order_by("-period_start", "-id").first()
        if latest_invoice:
            return latest_invoice.next_period_start - timedelta(days=15)
        if self.is_active and self.can_generate_initial_invoice():
            return self.first_billing_date - timedelta(days=15)
        return None

    def auto_ach_review_needed(self, as_of_date=None) -> bool:
        as_of_date = as_of_date or timezone.localdate()
        if not self.is_active or not self.auto_ach:
            return False
        issue_date = self.next_expected_issue_date()
        if not issue_date:
            return False
        review_start = issue_date.replace(day=1)
        if not (review_start <= as_of_date < issue_date):
            return False
        return self.open_balance_as_of(as_of_date) > Decimal("0.00")

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
    BILLING_STATUS_BILLABLE = "billable"
    BILLING_STATUS_HOLD = "hold"
    BILLING_STATUS_COMPLIMENTARY = "complimentary"
    BILLING_STATUS_INACTIVE = "inactive"
    BILLING_STATUS_CHOICES = (
        (BILLING_STATUS_BILLABLE, "Billable"),
        (BILLING_STATUS_HOLD, "Billing Hold"),
        (BILLING_STATUS_COMPLIMENTARY, "Complimentary"),
        (BILLING_STATUS_INACTIVE, "Inactive"),
    )

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="services")
    service_name = models.CharField(max_length=255, default="Alarm Monitoring Service")
    service_address1 = models.CharField(max_length=255)
    service_address2 = models.CharField(max_length=255, blank=True)
    activation_date = models.DateField(blank=True, null=True)
    billing_amount = models.DecimalField(max_digits=10, decimal_places=2)
    billing_status = models.CharField(max_length=20, choices=BILLING_STATUS_CHOICES, default=BILLING_STATUS_BILLABLE)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["customer__name", "service_name", "id"]

    def __str__(self) -> str:
        return f"{self.customer.account_number} - {self.service_name}"

    def save(self, *args, **kwargs):
        if not self.service_name:
            self.service_name = "Alarm Monitoring Service"
        if not self.service_address1:
            self.service_address1 = self.customer.billing_address1
        if not self.service_address2:
            self.service_address2 = self.customer.billing_address2
        if self.billing_status == self.BILLING_STATUS_INACTIVE:
            self.is_active = False
        super().save(*args, **kwargs)
        self.customer.ensure_initial_invoice()




