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


class PaymentSettlement(models.Model):
    MODE_TAX_INCLUSIVE = "tax_inclusive"
    MODE_CASH_TAX_WAIVED = "cash_tax_waived"
    MODE_CHOICES = (
        (MODE_TAX_INCLUSIVE, "Tax-Inclusive Full Settlement"),
        (MODE_CASH_TAX_WAIVED, "Cash / Tax Waived Settlement"),
    )

    payment = models.OneToOneField(Payment, on_delete=models.CASCADE, related_name="settlement")
    customer = models.ForeignKey("customers.Customer", on_delete=models.CASCADE, related_name="payment_settlements")
    mode = models.CharField(max_length=30, choices=MODE_CHOICES, default=MODE_TAX_INCLUSIVE)
    actual_received = models.DecimalField(max_digits=10, decimal_places=2)
    recognized_subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    recognized_tax = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    adjustment_subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    adjustment_tax = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"Settlement for payment {self.payment_id}"

    @property
    def total_adjustment(self) -> Decimal:
        return (Decimal(self.adjustment_subtotal) + Decimal(self.adjustment_tax)).quantize(Decimal("0.01"))

    @staticmethod
    def _split_tax_inclusive_amount(gross_amount: Decimal, tax_rate: Decimal):
        gross_amount = Decimal(gross_amount).quantize(Decimal("0.01"))
        tax_rate = Decimal(tax_rate or Decimal("0.00"))
        if tax_rate <= Decimal("0.00"):
            return gross_amount, Decimal("0.00")
        divisor = Decimal("1.00") + (tax_rate / Decimal("100.00"))
        recognized_subtotal = (gross_amount / divisor).quantize(Decimal("0.01"))
        recognized_tax = (gross_amount - recognized_subtotal).quantize(Decimal("0.01"))
        return recognized_subtotal, recognized_tax

    @classmethod
    def tax_inclusive_preview(cls, customer, payment_date, actual_received, exclude_payment_id=None):
        base_preview = Payment.allocation_preview(customer, payment_date, Decimal("0.00"), exclude_payment_id=exclude_payment_id)
        available_balance = Decimal(base_preview["available_balance"]).quantize(Decimal("0.01"))
        actual_received = Decimal(actual_received).quantize(Decimal("0.01"))
        if actual_received < Decimal("0.00"):
            actual_received = Decimal("0.00")
        if actual_received > available_balance:
            actual_received = available_balance
        shortfall = (available_balance - actual_received).quantize(Decimal("0.01"))
        recognized_subtotal, recognized_tax = cls._split_tax_inclusive_amount(actual_received, customer.tax_rate)
        adjustment_subtotal, adjustment_tax = cls._split_tax_inclusive_amount(shortfall, customer.tax_rate)
        return {
            "available_balance": available_balance,
            "actual_received": actual_received,
            "recognized_subtotal": recognized_subtotal,
            "recognized_tax": recognized_tax,
            "adjustment_subtotal": adjustment_subtotal,
            "adjustment_tax": adjustment_tax,
            "shortfall": shortfall,
        }

    @classmethod
    def cash_tax_waived_preview(cls, customer, payment_date, actual_received, exclude_payment_id=None):
        base_preview = Payment.allocation_preview(customer, payment_date, Decimal("0.00"), exclude_payment_id=exclude_payment_id)
        available_balance = Decimal(base_preview["available_balance"]).quantize(Decimal("0.01"))
        actual_received = Decimal(actual_received).quantize(Decimal("0.01"))
        if actual_received < Decimal("0.00"):
            actual_received = Decimal("0.00")
        if actual_received > available_balance:
            actual_received = available_balance
        shortfall = (available_balance - actual_received).quantize(Decimal("0.01"))
        return {
            "available_balance": available_balance,
            "actual_received": actual_received,
            "recognized_subtotal": actual_received,
            "recognized_tax": Decimal("0.00"),
            "adjustment_subtotal": Decimal("0.00"),
            "adjustment_tax": shortfall,
            "shortfall": shortfall,
        }

    @classmethod
    def _create_settlement_allocations(cls, settlement, payment, shortfall):
        if shortfall <= Decimal("0.00"):
            return settlement
        residual_preview = Payment.allocation_preview(
            payment.customer,
            payment.payment_date,
            Decimal("0.00"),
            exclude_payment_id=payment.pk,
        )
        for row in residual_preview["preview_rows"]:
            amount_due = Decimal(row["amount_due"]).quantize(Decimal("0.01"))
            if amount_due <= Decimal("0.00"):
                continue
            PaymentSettlementAllocation.objects.create(
                settlement=settlement,
                invoice=row["invoice"],
                amount=amount_due,
            )
        return settlement

    @classmethod
    def create_tax_inclusive_full_settlement(cls, payment, note=""):
        settlement_preview = cls.tax_inclusive_preview(
            payment.customer,
            payment.payment_date,
            payment.amount,
            exclude_payment_id=payment.pk,
        )
        shortfall = settlement_preview["shortfall"]
        settlement = cls.objects.create(
            payment=payment,
            customer=payment.customer,
            mode=cls.MODE_TAX_INCLUSIVE,
            actual_received=Decimal(payment.amount).quantize(Decimal("0.01")),
            recognized_subtotal=settlement_preview["recognized_subtotal"],
            recognized_tax=settlement_preview["recognized_tax"],
            adjustment_subtotal=settlement_preview["adjustment_subtotal"],
            adjustment_tax=settlement_preview["adjustment_tax"],
            note=note,
        )
        cls._create_settlement_allocations(settlement, payment, shortfall)
        Payment.refresh_customer_invoices(payment.customer)
        return settlement

    @classmethod
    def create_cash_tax_waived_settlement(cls, payment, note=""):
        settlement_preview = cls.cash_tax_waived_preview(
            payment.customer,
            payment.payment_date,
            payment.amount,
            exclude_payment_id=payment.pk,
        )
        shortfall = settlement_preview["shortfall"]
        settlement = cls.objects.create(
            payment=payment,
            customer=payment.customer,
            mode=cls.MODE_CASH_TAX_WAIVED,
            actual_received=Decimal(payment.amount).quantize(Decimal("0.01")),
            recognized_subtotal=settlement_preview["recognized_subtotal"],
            recognized_tax=settlement_preview["recognized_tax"],
            adjustment_subtotal=settlement_preview["adjustment_subtotal"],
            adjustment_tax=settlement_preview["adjustment_tax"],
            note=note,
        )
        cls._create_settlement_allocations(settlement, payment, shortfall)
        Payment.refresh_customer_invoices(payment.customer)
        return settlement

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


class PaymentSettlementAllocation(models.Model):
    settlement = models.ForeignKey(PaymentSettlement, on_delete=models.CASCADE, related_name="allocations")
    invoice = models.ForeignKey("billing.Invoice", on_delete=models.CASCADE, related_name="settlement_allocations")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["invoice__period_start", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["settlement", "invoice"],
                name="unique_invoice_settlement_allocation_per_payment",
            )
        ]

    def __str__(self) -> str:
        return f"Settlement {self.settlement_id} -> {self.invoice.invoice_number}: {self.amount}"

    def clean(self):
        if self.amount <= Decimal("0.00"):
            raise ValidationError("Settlement amount must be greater than zero.")



