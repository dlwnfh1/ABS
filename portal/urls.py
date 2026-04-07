from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import views

app_name = "portal"

urlpatterns = [
    path("login/", LoginView.as_view(template_name="portal/login.html", redirect_authenticated_user=True), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("", views.dashboard_view, name="dashboard"),
    path("payments/quick/", views.quick_payment_view, name="quick_payment"),
    path("payments/<int:payment_id>/attachment/", views.payment_attachment_view, name="payment_attachment"),
    path("payments/<int:payment_id>/receipt/pdf/", views.payment_receipt_pdf_view, name="payment_receipt_pdf"),
    path("payments/<int:payment_id>/receipt/print/", views.payment_receipt_print_view, name="payment_receipt_print"),
    path("invoices/", views.invoice_list_view, name="invoice_list"),
    path("invoices/<int:invoice_id>/pdf/", views.invoice_pdf_view, name="invoice_pdf"),
    path("invoices/<int:invoice_id>/print/", views.invoice_print_view, name="invoice_print"),
    path("reports/", views.report_index_view, name="report_index"),
    path("reports/ar-aging/", views.ar_aging_view, name="ar_aging"),
    path("reports/payments/", views.payments_report_view, name="payments_report"),
    path("reports/overdue-customers/", views.overdue_customers_view, name="overdue_customers"),
    path("reports/upcoming-billing/", views.upcoming_billing_view, name="upcoming_billing"),
    path("reports/customer-statement/", views.customer_statement_view, name="customer_statement"),
]
