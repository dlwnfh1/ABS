from datetime import date
from decimal import Decimal

from django.test import TestCase

from billing.models import Invoice
from customers.models import Customer, Service

from .models import Payment


class PaymentWorkflowTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(
            name="Payment Customer",
            account_number="P100",
            billing_address1="10 Main St",
            tax_rate=Decimal("10.00"),
            billing_term=3,
            first_billing_date=date(2026, 1, 1),
        )
        Service.objects.create(
            customer=self.customer,
            service_name="Monitoring",
            service_address1="10 Main St",
            activation_date=date(2026, 1, 1),
            billing_amount=Decimal("100.00"),
        )

    def test_payment_can_create_customer_credit_after_balance_is_paid(self):
        invoice = Invoice.objects.get(customer=self.customer, auto_generated=False)
        Payment.objects.create(
            customer=self.customer,
            amount=Decimal("110.00"),
            payment_date=date(2026, 1, 5),
            method=Payment.METHOD_CHECK,
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_PAID)

        credit_payment = Payment.objects.create(
            customer=self.customer,
            amount=Decimal("10.00"),
            payment_date=date(2026, 1, 6),
            method=Payment.METHOD_CASH,
        )

        self.assertEqual(credit_payment.applied_amount, Decimal("0.00"))
        self.assertEqual(credit_payment.unapplied_amount, Decimal("10.00"))

    def test_payment_over_open_balance_leaves_unapplied_credit(self):
        payment = Payment.objects.create(
            customer=self.customer,
            amount=Decimal("125.00"),
            payment_date=date(2026, 1, 5),
            method=Payment.METHOD_CHECK,
        )

        self.assertEqual(payment.applied_amount, Decimal("110.00"))
        self.assertEqual(payment.unapplied_amount, Decimal("15.00"))
        self.assertEqual(self.customer.credit_balance, Decimal("15.00"))

    def test_unapplied_credit_applies_when_next_invoice_is_generated(self):
        first_invoice = Invoice.objects.get(customer=self.customer, auto_generated=False)
        payment = Payment.objects.create(
            customer=self.customer,
            amount=Decimal("125.00"),
            payment_date=date(2026, 1, 5),
            method=Payment.METHOD_CHECK,
        )

        next_invoice = first_invoice.generate_next_invoice()
        payment.refresh_from_db()
        next_invoice.refresh_from_db()

        self.assertEqual(payment.applied_amount, Decimal("125.00"))
        self.assertEqual(payment.unapplied_amount, Decimal("0.00"))
        self.assertEqual(self.customer.credit_balance, Decimal("0.00"))
        self.assertEqual(next_invoice.allocated_amount_as_of(), Decimal("15.00"))
        self.assertEqual(next_invoice.total_due, Decimal("95.00"))
