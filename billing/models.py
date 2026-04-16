from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.utils import timezone


class Invoice(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_ISSUED = "issued"
    STATUS_PAID = "paid"
    STATUS_PARTIAL = "partial"
    STATUS_VOID = "void"

    STATUS_CHOICES = (
        (STATUS_DRAFT, "Draft"),
        (STATUS_ISSUED, "Issued"),
        (STATUS_PAID, "Paid"),
        (STATUS_PARTIAL, "Partially Paid"),
        (STATUS_VOID, "Void"),
    )

    customer = models.ForeignKey("customers.Customer", on_delete=models.CASCADE, related_name="invoices")
    invoice_number = models.CharField(max_length=50, unique=True, blank=True)
    period_start = models.DateField()
    period_end = models.DateField()
    issue_date = models.DateField(blank=True, null=True)
    due_date = models.DateField(blank=True, null=True)
    partial_payment = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    tax_rate = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("0.000"))
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total_due = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    auto_generated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-period_start", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "period_start", "period_end"],
                name="unique_invoice_period_per_customer",
            )
        ]

    def __str__(self) -> str:
        return self.invoice_number or f"Invoice {self.pk or 'new'}"

    def clean(self):
        if self.period_end < self.period_start:
            raise ValidationError("Billing period end must be on or after billing period start.")

    @property
    def next_period_start(self) -> date:
        return self.period_end + timedelta(days=1)

    @property
    def next_period_end(self) -> date:
        return add_months(self.next_period_start, self.customer.billing_term) - timedelta(days=1)

    @property
    def current_period_amount(self) -> Decimal:
        amount = self.items.filter(line_type=InvoiceItem.LINE_CURRENT_PERIOD).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        return Decimal(amount).quantize(Decimal("0.01"))

    @property
    def current_period_tax(self) -> Decimal:
        tax = (self.current_period_amount * self.tax_rate / Decimal("100")).quantize(Decimal("0.01"))
        return max(tax, Decimal("0.00"))

    @property
    def current_period_total(self) -> Decimal:
        return (self.current_period_amount + self.current_period_tax).quantize(Decimal("0.01"))

    @property
    def last_payment(self):
        return self.customer.payments.filter(is_voided=False).order_by("-payment_date", "-id").first()

    @property
    def last_payment_summary(self) -> str:
        payment = self.last_payment
        if not payment:
            return ""
        reference = f" #{payment.reference_number}" if payment.reference_number else ""
        return (
            f"Last Payment was ${payment.amount:.2f} with {payment.get_method_display()}{reference} "
            f"on the date of {payment.payment_date:%m-%d-%Y}"
        )

    @property
    def customer_payments(self):
        from payments.models import Payment

        return Payment.objects.filter(customer=self.customer, is_voided=False)

    def allocated_amount_as_of(self, as_of_date=None, exclude_payment_id=None) -> Decimal:
        prefetched_allocations = getattr(self, "_prefetched_valid_allocations", None)
        if prefetched_allocations is not None:
            total = Decimal("0.00")
            for allocation in prefetched_allocations:
                payment = allocation.payment
                if as_of_date and payment.payment_date > as_of_date:
                    continue
                if exclude_payment_id and allocation.payment_id == exclude_payment_id:
                    continue
                total += Decimal(allocation.amount)
            return Decimal(total).quantize(Decimal("0.01"))

        allocations = self.allocations.filter(payment__is_voided=False)
        if as_of_date:
            allocations = allocations.filter(payment__payment_date__lte=as_of_date)
        if exclude_payment_id:
            allocations = allocations.exclude(payment_id=exclude_payment_id)
        total = allocations.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        return Decimal(total).quantize(Decimal("0.01"))

    def base_paid_as_of(self, as_of_date=None, exclude_payment_id=None) -> Decimal:
        allocated = self.allocated_amount_as_of(as_of_date=as_of_date, exclude_payment_id=exclude_payment_id)
        return min(self.current_period_amount, allocated).quantize(Decimal("0.01"))

    def outstanding_amount_as_of(self, as_of_date: date) -> Decimal:
        outstanding = self.current_period_amount - self.base_paid_as_of(as_of_date=as_of_date)
        return max(outstanding.quantize(Decimal("0.01")), Decimal("0.00"))

    def statement_base_totals(self, exclude_payment_id=None):
        self.rebuild_items()
        line_total = self.items.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        prior_invoices = (
            Invoice.objects.filter(customer=self.customer, period_end__lt=self.period_start)
            .exclude(status=self.STATUS_VOID)
            .order_by("period_start", "id")
        )
        prior_base_paid = Decimal("0.00")
        for prior_invoice in prior_invoices:
            prior_base_paid += prior_invoice.base_paid_as_of(
                as_of_date=self.issue_date,
                exclude_payment_id=exclude_payment_id,
            )

        subtotal = (Decimal(line_total) - prior_base_paid).quantize(Decimal("0.01"))
        if subtotal < Decimal("0.00"):
            subtotal = Decimal("0.00")
        tax_rate = self.customer.tax_rate
        tax_amount = (subtotal * tax_rate / Decimal("100")).quantize(Decimal("0.01"))
        gross_total = (subtotal + tax_amount).quantize(Decimal("0.01"))
        return {
            "line_total": Decimal(line_total).quantize(Decimal("0.01")),
            "partial_payment": prior_base_paid.quantize(Decimal("0.01")),
            "subtotal": subtotal,
            "tax_rate": tax_rate,
            "tax_amount": tax_amount,
            "gross_total": gross_total,
        }

    def unique_amount_due_for_allocation(self, as_of_date=None, exclude_payment_id=None) -> Decimal:
        allocated = self.allocated_amount_as_of(as_of_date=as_of_date, exclude_payment_id=exclude_payment_id)
        due = self.current_period_total - allocated
        return max(due.quantize(Decimal("0.01")), Decimal("0.00"))

    def amount_due_for_allocation(self, as_of_date=None, exclude_payment_id=None) -> Decimal:
        return self.unique_amount_due_for_allocation(
            as_of_date=as_of_date,
            exclude_payment_id=exclude_payment_id,
        )

    def rebuild_items(self):
        if not self.pk:
            return

        self.items.all().delete()

        prior_invoices = (
            Invoice.objects.filter(customer=self.customer, period_end__lt=self.period_start)
            .exclude(status=self.STATUS_VOID)
            .order_by("period_start", "id")
        )

        for prior_invoice in prior_invoices:
            if prior_invoice.outstanding_amount_as_of(self.issue_date) > Decimal("0.00"):
                InvoiceItem.objects.create(
                    invoice=self,
                    line_type=InvoiceItem.LINE_CARRYOVER,
                    description=f"Billing Period {prior_invoice.period_start:%m/%d/%y} - {prior_invoice.period_end:%m/%d/%y}",
                    period_start=prior_invoice.period_start,
                    period_end=prior_invoice.period_end,
                    amount=prior_invoice.current_period_amount,
                )

        InvoiceItem.objects.create(
            invoice=self,
            line_type=InvoiceItem.LINE_CURRENT_PERIOD,
            description=f"Billing Period {self.period_start:%m/%d/%y} - {self.period_end:%m/%d/%y}",
            period_start=self.period_start,
            period_end=self.period_end,
            amount=self.customer.current_billing_amount,
        )

    def refresh_statement(self, commit: bool = True):
        totals = self.statement_base_totals()

        self.partial_payment = totals["partial_payment"]
        self.subtotal = totals["subtotal"]
        self.tax_rate = totals["tax_rate"]
        self.tax_amount = totals["tax_amount"]
        allocated_to_self = self.allocated_amount_as_of()
        self.total_due = max((totals["gross_total"] - allocated_to_self).quantize(Decimal("0.01")), Decimal("0.00"))
        if self.status != self.STATUS_VOID:
            if self.total_due <= Decimal("0.00"):
                self.status = self.STATUS_PAID
            elif allocated_to_self > Decimal("0.00"):
                self.status = self.STATUS_PARTIAL
            else:
                self.status = self.STATUS_ISSUED

        if commit:
            Invoice.objects.filter(pk=self.pk).update(
                partial_payment=self.partial_payment,
                subtotal=self.subtotal,
                tax_rate=self.tax_rate,
                tax_amount=self.tax_amount,
                total_due=self.total_due,
                status=self.status,
                updated_at=timezone.now(),
            )

    def save(self, *args, **kwargs):
        create_followup = kwargs.pop("create_followup", True)
        if not self.invoice_number:
            self.invoice_number = self.build_invoice_number()
        if not self.issue_date:
            self.issue_date = self.period_start - timedelta(days=15)
        if not self.due_date:
            self.due_date = self.period_start
        self.full_clean()
        super().save(*args, **kwargs)
        self.refresh_statement(commit=True)
        if create_followup:
            self.refresh_future_invoices()

    def build_invoice_number(self) -> str:
        base_number = f"INV-{self.customer.account_number}-{self.period_start:%Y%m%d}"
        if not Invoice.objects.filter(invoice_number=base_number).exists():
            return base_number

        suffix = 2
        while Invoice.objects.filter(invoice_number=f"{base_number}-{suffix:02d}").exists():
            suffix += 1
        return f"{base_number}-{suffix:02d}"

    def generate_next_invoice(self):
        next_start = self.next_period_start
        next_end = self.next_period_end
        if Invoice.objects.filter(
            customer=self.customer,
            period_start=next_start,
            period_end=next_end,
        ).exists():
            return

        next_invoice = Invoice(
            customer=self.customer,
            period_start=next_start,
            period_end=next_end,
            issue_date=next_start - timedelta(days=15),
            due_date=next_start,
            status=self.STATUS_ISSUED,
            auto_generated=True,
        )
        next_invoice.save(create_followup=False)
        return next_invoice

    @classmethod
    def generate_all_due_for_customer(cls, customer, force=False, as_of_date=None):
        as_of_date = as_of_date or timezone.localdate()
        created_invoices = []

        while True:
            invoice, status, _message = cls.generate_for_customer(
                customer,
                force=force,
                as_of_date=as_of_date,
            )
            if status != "created" or not invoice:
                final_status = status
                break
            created_invoices.append(invoice)

        if created_invoices:
            return created_invoices, "created", f"Generated {len(created_invoices)} invoice(s)."
        return created_invoices, final_status, _message

    @classmethod
    def generate_due_invoices(cls, as_of_date=None):
        as_of_date = as_of_date or timezone.localdate()
        generated = []
        for candidate in cls.get_generation_candidates(as_of_date=as_of_date):
            if not candidate["is_due"]:
                continue
            invoice, status, _ = cls.generate_for_customer(candidate["customer"], as_of_date=as_of_date)
            if invoice and status == "created":
                generated.append(invoice)
        return generated

    @classmethod
    def get_generation_candidates(cls, as_of_date=None):
        as_of_date = as_of_date or timezone.localdate()
        customer_model = cls._meta.get_field("customer").related_model
        candidates = []
        for customer in customer_model.objects.filter(is_active=True).order_by("name", "account_number"):
            latest_invoice = customer.invoices.exclude(status=cls.STATUS_VOID).order_by("-period_start", "-id").first()
            if latest_invoice is None:
                if not customer.first_billing_date or not customer.billable_services.exists():
                    continue
                period_start = customer.first_billing_date
                period_end = add_months(period_start, customer.billing_term) - timedelta(days=1)
                issue_date = period_start - timedelta(days=15)
                due_date = period_start
            else:
                period_start = latest_invoice.next_period_start
                period_end = latest_invoice.next_period_end
                issue_date = period_start - timedelta(days=15)
                due_date = period_start

            existing_invoice = cls.objects.filter(
                customer=customer,
                period_start=period_start,
                period_end=period_end,
            ).first()
            is_due = issue_date <= as_of_date
            if existing_invoice:
                status = "already_exists"
                message = f"Already issued: {existing_invoice.invoice_number}"
            elif is_due:
                status = "ready"
                message = "Ready to generate."
            else:
                status = "not_due"
                message = f"Available on {issue_date:%Y-%m-%d}"

            candidates.append(
                {
                    "customer": customer,
                    "latest_invoice": latest_invoice,
                    "period_start": period_start,
                    "period_end": period_end,
                    "issue_date": issue_date,
                    "due_date": due_date,
                    "existing_invoice": existing_invoice,
                    "is_due": is_due,
                    "status": status,
                    "message": message,
                }
            )
        return candidates

    @classmethod
    def generate_for_customer(cls, customer, force=False, as_of_date=None):
        as_of_date = as_of_date or timezone.localdate()
        latest_invoice = customer.invoices.exclude(status=cls.STATUS_VOID).order_by("-period_start", "-id").first()

        if latest_invoice is None:
            if not customer.can_generate_initial_invoice():
                return None, "missing_setup", "Customer needs a first billing date and an active service."
            period_start = customer.first_billing_date
            period_end = add_months(period_start, customer.billing_term) - timedelta(days=1)
            issue_date = period_start - timedelta(days=15)
            existing_invoice = cls.objects.filter(customer=customer, period_start=period_start, period_end=period_end).first()
            if existing_invoice:
                return existing_invoice, "already_exists", f"Already issued: {existing_invoice.invoice_number}"
            if issue_date > as_of_date and not force:
                return None, "not_due", f"Available on {issue_date:%Y-%m-%d}"
            return customer.ensure_initial_invoice(), "created", "Initial invoice created."

        if not customer.can_generate_initial_invoice():
            return None, "missing_setup", "Customer needs a first billing date and a billable active service."

        period_start = latest_invoice.next_period_start
        period_end = latest_invoice.next_period_end
        issue_date = period_start - timedelta(days=15)
        existing_invoice = cls.objects.filter(customer=customer, period_start=period_start, period_end=period_end).first()
        if existing_invoice:
            return existing_invoice, "already_exists", f"Already issued: {existing_invoice.invoice_number}"
        if issue_date > as_of_date and not force:
            return None, "not_due", f"Available on {issue_date:%Y-%m-%d}"
        return latest_invoice.generate_next_invoice(), "created", "Invoice generated."

    def refresh_future_invoices(self):
        future_invoices = Invoice.objects.filter(customer=self.customer, period_start__gt=self.period_start).order_by("period_start", "id")
        for future_invoice in future_invoices:
            future_invoice.refresh_statement(commit=True)


class InvoiceItem(models.Model):
    LINE_CURRENT_PERIOD = "current_period"
    LINE_CARRYOVER = "carryover"

    LINE_TYPE_CHOICES = (
        (LINE_CURRENT_PERIOD, "Current Period"),
        (LINE_CARRYOVER, "Carryover"),
    )

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    line_type = models.CharField(max_length=20, choices=LINE_TYPE_CHOICES)
    description = models.CharField(max_length=255)
    period_start = models.DateField()
    period_end = models.DateField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["period_start", "id"]

    def __str__(self) -> str:
        return f"{self.invoice.invoice_number} - {self.description}"


def add_months(value: date, months: int) -> date:
    year = value.year + ((value.month - 1 + months) // 12)
    month = ((value.month - 1 + months) % 12) + 1
    month_lengths = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(value.day, month_lengths[month - 1])
    return date(year, month, day)

