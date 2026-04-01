from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Payment(models.Model):
    METHOD_CASH = "cash"
    METHOD_CHECK = "check"
    METHOD_CREDIT_CARD = "credit_card"
    METHOD_ACH = "ach"
    METHOD_OTHER = "other"

    METHOD_CHOICES = (
        (METHOD_CASH, "Cash"),
        (METHOD_CHECK, "Check"),
        (METHOD_CREDIT_CARD, "Credit Card"),
        (METHOD_ACH, "ACH"),
        (METHOD_OTHER, "Other"),
    )

    customer = models.ForeignKey("customers.Customer", on_delete=models.CASCADE, related_name="payments")
    payment_date = models.DateField(default=timezone.localdate)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default=METHOD_CHECK)
    reference_number = models.CharField(max_length=100, blank=True)
    note = models.TextField(blank=True)
    scanned_check_path = models.CharField(max_length=1000, blank=True)
    scanned_check_uploaded_at = models.DateTimeField(blank=True, null=True)
    is_voided = models.BooleanField(default=False)
    voided_at = models.DateTimeField(blank=True, null=True)
    void_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-payment_date", "-id"]

    def __str__(self) -> str:
        suffix = " [VOID]" if self.is_voided else ""
        return f"{self.customer.account_number} - {self.amount}{suffix}"

    @property
    def applied_amount(self) -> Decimal:
        if self.is_voided:
            return Decimal("0.00")
        total = self.allocations.aggregate(total=models.Sum("amount"))["total"] or Decimal("0.00")
        return Decimal(total).quantize(Decimal("0.01"))

    @property
    def unapplied_amount(self) -> Decimal:
        if self.is_voided:
            return Decimal("0.00")
        return (Decimal(self.amount) - self.applied_amount).quantize(Decimal("0.01"))

    def clean(self):
        from billing.models import Invoice

        if self.is_voided:
            return
        if self.amount <= Decimal("0.00"):
            raise ValidationError("Payment amount must be greater than zero.")
        if not self.customer_id:
            raise ValidationError("Customer is required.")

        invoices = (
            Invoice.objects.filter(customer=self.customer)
            .exclude(status=Invoice.STATUS_VOID)
            .order_by("period_start", "id")
        )
        available_balance = Decimal("0.00")
        for invoice in invoices:
            if invoice.issue_date and invoice.issue_date > self.payment_date:
                continue
            available_balance += invoice.amount_due_for_allocation(
                as_of_date=self.payment_date,
                exclude_payment_id=self.pk,
            )

        available_balance = available_balance.quantize(Decimal("0.01"))
        if available_balance <= Decimal("0.00"):
            raise ValidationError("Customer has no open invoices available for this payment date.")
        if Decimal(self.amount) > available_balance:
            raise ValidationError(f"Payment exceeds the customer's open balance of ${available_balance:.2f}.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
        self.reallocate()

    def delete(self, *args, **kwargs):
        customer = self.customer
        super().delete(*args, **kwargs)
        self.refresh_customer_invoices(customer)

    def void(self, reason=""):
        if self.is_voided:
            return
        self.is_voided = True
        self.voided_at = timezone.now()
        self.void_reason = reason or ""
        Payment.objects.filter(pk=self.pk).update(
            is_voided=True,
            voided_at=self.voided_at,
            void_reason=self.void_reason,
        )
        self.allocations.all().delete()
        self.refresh_customer_invoices(self.customer)

    @property
    def has_scanned_check(self):
        return bool(self.scanned_check_path)

    def scanned_check_filename(self):
        if not self.scanned_check_path:
            return ""
        return Path(self.scanned_check_path).name

    @classmethod
    def allocation_preview(cls, customer, payment_date, amount, exclude_payment_id=None):
        from billing.models import Invoice

        amount = Decimal(amount).quantize(Decimal("0.01"))
        invoices = list(
            Invoice.objects.filter(customer=customer)
            .exclude(status=Invoice.STATUS_VOID)
            .order_by("period_start", "id")
        )

        available_balance = Decimal("0.00")
        preview_rows = []
        remaining = amount

        for invoice in invoices:
            if invoice.issue_date and invoice.issue_date > payment_date:
                continue
            amount_due = invoice.amount_due_for_allocation(
                as_of_date=payment_date,
                exclude_payment_id=exclude_payment_id,
            )
            if amount_due <= Decimal("0.00"):
                continue

            available_balance += amount_due
            applied_amount = Decimal("0.00")
            if remaining > Decimal("0.00"):
                applied_amount = min(remaining, amount_due).quantize(Decimal("0.01"))
                remaining -= applied_amount

            preview_rows.append(
                {
                    "invoice": invoice,
                    "amount_due": amount_due.quantize(Decimal("0.01")),
                    "applied_amount": applied_amount,
                    "remaining_after": max(remaining, Decimal("0.00")).quantize(Decimal("0.01")),
                }
            )

        return {
            "available_balance": available_balance.quantize(Decimal("0.01")),
            "preview_rows": preview_rows,
            "unapplied_amount": max(remaining, Decimal("0.00")).quantize(Decimal("0.01")),
        }

    def reallocate(self):
        from billing.models import Invoice

        self.allocations.all().delete()
        if self.is_voided:
            self.refresh_customer_invoices(self.customer)
            return
        invoices = list(
            Invoice.objects.filter(customer=self.customer)
            .exclude(status=Invoice.STATUS_VOID)
            .order_by("period_start", "id")
        )

        for invoice in invoices:
            invoice.refresh_statement(commit=True)

        remaining = Decimal(self.amount)
        for invoice in invoices:
            if remaining <= Decimal("0.00"):
                break
            if invoice.issue_date and invoice.issue_date > self.payment_date:
                continue
            amount_due = invoice.amount_due_for_allocation(
                as_of_date=self.payment_date,
                exclude_payment_id=self.pk,
            )
            if amount_due <= Decimal("0.00"):
                continue

            applied_amount = min(remaining, amount_due).quantize(Decimal("0.01"))
            PaymentAllocation.objects.create(
                payment=self,
                invoice=invoice,
                amount=applied_amount,
            )
            remaining -= applied_amount

        self.refresh_customer_invoices(self.customer)

    @staticmethod
    def refresh_customer_invoices(customer):
        from billing.models import Invoice

        invoices = (
            Invoice.objects.filter(customer=customer)
            .exclude(status=Invoice.STATUS_VOID)
            .order_by("period_start", "id")
        )
        for invoice in invoices:
            invoice.refresh_statement(commit=True)


class PaymentAllocation(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="allocations")
    invoice = models.ForeignKey("billing.Invoice", on_delete=models.CASCADE, related_name="allocations")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["invoice__period_start", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "invoice"],
                name="unique_invoice_allocation_per_payment",
            )
        ]

    def __str__(self) -> str:
        return f"{self.payment_id} -> {self.invoice.invoice_number}: {self.amount}"

    def clean(self):
        if self.amount <= Decimal("0.00"):
            raise ValidationError("Allocated amount must be greater than zero.")
