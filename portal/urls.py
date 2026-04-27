from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeDoneView, PasswordChangeView
from django.urls import path
from django.urls import reverse_lazy

from . import views

app_name = "portal"

urlpatterns = [
    path("login/", LoginView.as_view(template_name="portal/login.html", redirect_authenticated_user=True), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path(
        "password/change/",
        PasswordChangeView.as_view(
            template_name="portal/password_change.html",
            success_url=reverse_lazy("portal:password_change_done"),
        ),
        name="password_change",
    ),
    path(
        "password/change/done/",
        PasswordChangeDoneView.as_view(template_name="portal/password_change_done.html"),
        name="password_change_done",
    ),
    path("", views.dashboard_view, name="dashboard"),
    path("customers/", views.customer_list_view, name="customer_list"),
    path("customers/new/", views.customer_create_view, name="customer_create"),
    path("customers/<int:customer_id>/edit/", views.customer_edit_view, name="customer_edit"),
    path("payments/quick/", views.quick_payment_view, name="quick_payment"),
    path("payments/<int:payment_id>/attachment/", views.payment_attachment_view, name="payment_attachment"),
    path("payments/<int:payment_id>/receipt/pdf/", views.payment_receipt_pdf_view, name="payment_receipt_pdf"),
    path("payments/<int:payment_id>/receipt/print/", views.payment_receipt_print_view, name="payment_receipt_print"),
    path("invoices/", views.invoice_list_view, name="invoice_list"),
    path("invoices/saved/", views.saved_invoice_list_view, name="saved_invoice_list"),
    path("invoices/saved/file/", views.saved_invoice_file_view, name="saved_invoice_file"),
    path("invoices/saved/merged/pdf/", views.saved_invoice_merged_pdf_view, name="saved_invoice_merged_pdf"),
    path("invoices/saved/merged/print/", views.saved_invoice_merged_print_view, name="saved_invoice_merged_print"),
    path("invoices/saved/batch/toggle-printed/", views.saved_invoice_batch_print_toggle_view, name="saved_invoice_batch_toggle_printed"),
    path("invoices/<int:invoice_id>/pdf/", views.invoice_pdf_view, name="invoice_pdf"),
    path("invoices/<int:invoice_id>/print/", views.invoice_print_view, name="invoice_print"),
    path("reports/", views.report_index_view, name="report_index"),
    path("reports/ar-aging/", views.ar_aging_view, name="ar_aging"),
    path("reports/payments/", views.payments_report_view, name="payments_report"),
    path("reports/overdue-customers/", views.overdue_customers_view, name="overdue_customers"),
    path("reports/upcoming-billing/", views.upcoming_billing_view, name="upcoming_billing"),
    path("reports/non-billable-customers/", views.non_billable_customers_view, name="non_billable_customers"),
    path("reports/auto-ach-review/", views.auto_ach_review_view, name="auto_ach_review"),
    path("reports/customer-statement/", views.customer_statement_view, name="customer_statement"),
]
