from io import BytesIO
from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase

from .admin import CustomerAdmin
from .models import Customer, Service


class CustomerCsvAdminTests(TestCase):
    def setUp(self):
        self.admin = CustomerAdmin(Customer, AdminSite())
        self.factory = RequestFactory()

    def test_import_csv_creates_customer_and_service(self):
        content = "\n".join(
            [
                "account_number,customer_name,billing_address1,billing_address2,phone_number,billing_term,tax_rate,first_billing_date,customer_is_active,service_name,service_address1,service_address2,activation_date,billing_amount,service_billing_status,service_is_active",
                "A100,Acme Corp,1 Main St,,123-456-7890,3,8.25,01-01-2026,1,Monitoring Service,10 Service Rd,,01-01-2026,100.00,billable,1",
            ]
        )
        upload = SimpleUploadedFile("customers.csv", content.encode("utf-8"), content_type="text/csv")

        created_customers, updated_customers, created_services, updated_services = self.admin._import_csv_file(upload)

        self.assertEqual((created_customers, updated_customers, created_services, updated_services), (1, 0, 1, 0))
        customer = Customer.objects.get(account_number="A100")
        service = Service.objects.get(customer=customer, service_name="Monitoring Service")
        self.assertEqual(customer.name, "Acme Corp")
        self.assertEqual(service.billing_amount, Decimal("100.00"))

    def test_export_csv_contains_expected_headers(self):
        customer = Customer.objects.create(
            name="Acme Corp",
            account_number="A100",
            billing_address1="1 Main St",
            billing_term=3,
            tax_rate="8.25",
        )
        Service.objects.create(
            customer=customer,
            service_name="Monitoring Service",
            service_address1="10 Service Rd",
            activation_date="2026-01-01",
            billing_amount="100.00",
        )
        request = self.factory.get("/admin/customers/customer/export-csv/")

        response = self.admin.export_csv_view(request)
        body = response.content.decode("utf-8")

        self.assertIn("account_number,customer_name,billing_address1", body)
        self.assertIn("A100,Acme Corp,1 Main St", body)
        self.assertIn("01-01-2026", body)

    def test_import_csv_accepts_slash_dates(self):
        content = "\n".join(
            [
                "account_number,customer_name,billing_address1,billing_address2,phone_number,billing_term,tax_rate,first_billing_date,customer_is_active,service_name,service_address1,service_address2,activation_date,billing_amount,service_billing_status,service_is_active",
                "B200,Slash Date Co,2 Main St,,234-567-8901,3,8.25,8/1/2025,1,Monitoring Service,20 Service Rd,,2/4/2013,90.00,billable,1",
            ]
        )
        upload = SimpleUploadedFile("customers_slash.csv", content.encode("utf-8"), content_type="text/csv")

        self.admin._import_csv_file(upload)

        customer = Customer.objects.get(account_number="B200")
        service = Service.objects.get(customer=customer, service_name="Monitoring Service")
        self.assertEqual(str(customer.first_billing_date), "2025-08-01")
        self.assertEqual(str(service.activation_date), "2013-02-04")

    def test_import_csv_allows_blank_activation_date(self):
        content = "\n".join(
            [
                "account_number,customer_name,billing_address1,billing_address2,phone_number,billing_term,tax_rate,first_billing_date,customer_is_active,service_name,service_address1,service_address2,activation_date,billing_amount,service_billing_status,service_is_active",
                "C300,Blank Activation Co,3 Main St,,345-678-9012,3,8.25,03-01-2026,1,Monitoring Service,30 Service Rd,,,95.00,billable,1",
            ]
        )
        upload = SimpleUploadedFile("customers_blank_activation.csv", content.encode("utf-8"), content_type="text/csv")

        self.admin._import_csv_file(upload)

        service = Service.objects.get(customer__account_number="C300", service_name="Monitoring Service")
        self.assertIsNone(service.activation_date)
