from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
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

    def test_payment_requires_open_customer_balance(self):
        invoice = Invoice.objects.get(customer=self.customer, auto_generated=False)
        Payment.objects.create(
            customer=self.customer,
            amount=Decimal("110.00"),
            payment_date=date(2026, 1, 5),
            method=Payment.METHOD_CHECK,
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_PAID)

        with self.assertRaises(ValidationError):
            Payment.objects.create(
                customer=self.customer,
                amount=Decimal("10.00"),
                payment_date=date(2026, 1, 6),
                method=Payment.METHOD_CASH,
            )

    def test_payment_cannot_exceed_open_balance(self):
        with self.assertRaises(ValidationError):
            Payment.objects.create(
                customer=self.customer,
                amount=Decimal("999.00"),
                payment_date=date(2026, 1, 5),
                method=Payment.METHOD_CHECK,
            )
