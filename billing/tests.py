from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from customers.models import Customer, Service
from payments.models import Payment

from .models import Invoice, InvoiceItem


class BillingWorkflowTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(
            name="ABC Telecom",
            account_number="A100",
            billing_address1="1 Main St",
            tax_rate=Decimal("10.00"),
            billing_term=3,
            first_billing_date=date(2026, 1, 1),
            email_address="billing@example.com",
        )
        self.service = Service.objects.create(
            customer=self.customer,
            service_name="Internet",
            service_address1="1 Main St",
            activation_date=date(2026, 1, 1),
            billing_amount=Decimal("100.00"),
        )

    def test_customer_service_creation_generates_initial_invoice(self):
        invoice = Invoice.objects.get(customer=self.customer, auto_generated=False)
        self.assertEqual(invoice.period_start, date(2026, 1, 1))
        self.assertEqual(invoice.period_end, date(2026, 3, 31))
        self.assertEqual(invoice.issue_date, date(2025, 12, 17))
        self.assertEqual(invoice.due_date, date(2026, 1, 1))
        self.assertEqual(invoice.invoice_number, "INV-A100-20260101")

    def test_due_invoice_generation_creates_next_invoice(self):
        invoice = Invoice.objects.get(customer=self.customer, auto_generated=False)
        next_invoice = invoice.generate_next_invoice()
        self.assertEqual(next_invoice.period_start, date(2026, 4, 1))
        self.assertEqual(next_invoice.period_end, date(2026, 6, 30))
        self.assertEqual(next_invoice.issue_date, date(2026, 3, 17))
        self.assertEqual(next_invoice.due_date, date(2026, 4, 1))

        items = list(next_invoice.items.order_by("period_start"))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].line_type, InvoiceItem.LINE_CARRYOVER)
        self.assertEqual(items[0].amount, Decimal("100.00"))
        self.assertEqual(items[1].line_type, InvoiceItem.LINE_CURRENT_PERIOD)
        self.assertEqual(items[1].amount, Decimal("100.00"))
        self.assertEqual(next_invoice.partial_payment, Decimal("0.00"))
        self.assertEqual(next_invoice.subtotal, Decimal("200.00"))
        self.assertEqual(next_invoice.tax_amount, Decimal("20.00"))
        self.assertEqual(next_invoice.total_due, Decimal("220.00"))

    def test_payment_updates_future_invoice_statement(self):
        invoice = Invoice.objects.get(customer=self.customer, auto_generated=False)
        next_invoice = invoice.generate_next_invoice()

        Payment.objects.create(
            customer=self.customer,
            amount=Decimal("50.00"),
            payment_date=date(2026, 3, 3),
            method=Payment.METHOD_CHECK,
            reference_number="CHK-1",
        )

        next_invoice.refresh_from_db()
        items = list(next_invoice.items.order_by("period_start"))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].amount, Decimal("100.00"))
        self.assertEqual(items[1].amount, Decimal("100.00"))
        self.assertEqual(next_invoice.partial_payment, Decimal("50.00"))
        self.assertEqual(next_invoice.subtotal, Decimal("150.00"))
        self.assertEqual(next_invoice.tax_amount, Decimal("15.00"))
        self.assertEqual(next_invoice.total_due, Decimal("165.00"))
        self.assertEqual(
            next_invoice.last_payment_summary,
            "Last Payment was $50.00 with Check #CHK-1 on the date of 03-03-2026",
        )

    def test_payment_auto_allocates_oldest_open_invoices(self):
        first_invoice = Invoice.objects.get(customer=self.customer, auto_generated=False)
        next_invoice = first_invoice.generate_next_invoice()

        payment = Payment.objects.create(
            customer=self.customer,
            amount=Decimal("120.00"),
            payment_date=date(2026, 3, 20),
            method=Payment.METHOD_CHECK,
            reference_number="CHK-2",
        )

        allocations = list(payment.allocations.order_by("invoice__period_start"))
        self.assertEqual(len(allocations), 2)
        self.assertEqual(allocations[0].invoice, first_invoice)
        self.assertEqual(allocations[0].amount, Decimal("110.00"))
        self.assertEqual(allocations[1].invoice, next_invoice)
        self.assertEqual(allocations[1].amount, Decimal("10.00"))

        first_invoice.refresh_from_db()
        next_invoice.refresh_from_db()
        self.assertEqual(first_invoice.status, Invoice.STATUS_PAID)
        self.assertEqual(first_invoice.total_due, Decimal("0.00"))
        self.assertEqual(next_invoice.subtotal, Decimal("200.00"))
        self.assertEqual(next_invoice.tax_amount, Decimal("20.00"))
        self.assertEqual(next_invoice.total_due, Decimal("210.00"))
        items = list(next_invoice.items.order_by("period_start"))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].line_type, InvoiceItem.LINE_CARRYOVER)
        self.assertEqual(items[0].amount, Decimal("100.00"))

    def test_customer_open_balance_matches_latest_invoice_less_later_payments(self):
        first_invoice = Invoice.objects.get(customer=self.customer, auto_generated=False)
        second_invoice = first_invoice.generate_next_invoice()
        third_invoice = second_invoice.generate_next_invoice()

        Payment.objects.create(
            customer=self.customer,
            amount=Decimal("45.00"),
            payment_date=date(2026, 3, 23),
            method=Payment.METHOD_CHECK,
            reference_number="CHK-3",
        )

        self.assertEqual(self.customer.open_balance_as_of(date(2026, 3, 23)), Decimal("175.00"))

    def test_generate_due_invoices_only_after_issue_date(self):
        invoice = Invoice.objects.get(customer=self.customer, auto_generated=False)
        generated = Invoice.generate_due_invoices(as_of_date=date(2026, 3, 16))
        self.assertEqual(generated, [])

        generated = Invoice.generate_due_invoices(as_of_date=date(2026, 3, 17))
        self.assertEqual(len(generated), 1)
        self.assertEqual(generated[0].period_start, date(2026, 4, 1))

    def test_force_generation_allows_early_issue(self):
        invoice = Invoice.objects.get(customer=self.customer, auto_generated=False)
        generated_invoice, status, message = Invoice.generate_for_customer(
            self.customer,
            force=True,
            as_of_date=date(2026, 3, 16),
        )
        self.assertEqual(status, "created")
        self.assertEqual(generated_invoice.period_start, invoice.next_period_start)
        self.assertEqual(message, "Invoice generated.")

    def test_second_generation_blocks_until_next_issue_date(self):
        first_generated, status, _ = Invoice.generate_for_customer(
            self.customer,
            force=True,
            as_of_date=date(2026, 3, 16),
        )
        self.assertEqual(status, "created")

        second_generated, status, message = Invoice.generate_for_customer(
            self.customer,
            force=False,
            as_of_date=date(2026, 3, 16),
        )
        self.assertIsNone(second_generated)
        self.assertEqual(status, "not_due")
        self.assertIn("Available on", message)

    def test_generate_all_due_for_customer_creates_catch_up_invoices(self):
        customer = Customer.objects.create(
            name="Catchup Telecom",
            account_number="C200",
            billing_address1="2 Main St",
            tax_rate=Decimal("10.00"),
            billing_term=3,
            first_billing_date=date(2025, 5, 1),
            email_address="catchup@example.com",
        )
        Service.objects.create(
            customer=customer,
            service_name="Monitoring",
            service_address1="2 Main St",
            activation_date=date(2025, 5, 1),
            billing_amount=Decimal("100.00"),
        )

        created_invoices, status, message = Invoice.generate_all_due_for_customer(
            customer,
            force=False,
            as_of_date=date(2026, 3, 17),
        )

        self.assertEqual(status, "created")
        self.assertEqual(len(created_invoices), 3)
        self.assertEqual(message, "Generated 3 invoice(s).")
        periods = [(invoice.period_start, invoice.period_end) for invoice in created_invoices]
        self.assertEqual(
            periods,
            [
                (date(2025, 8, 1), date(2025, 10, 31)),
                (date(2025, 11, 1), date(2026, 1, 31)),
                (date(2026, 2, 1), date(2026, 4, 30)),
            ],
        )

# Create your tests here.
