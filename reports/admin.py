from decimal import Decimal

from django.contrib import admin
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone

from billing.models import Invoice
from payments.models import Payment

from .models import ReportCenter


@admin.register(ReportCenter)
class ReportCenterAdmin(admin.ModelAdmin):
    change_list_template = "admin/reports/reportcenter/change_list.html"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return ReportCenter.objects.none()

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path("ar-aging/", self.admin_site.admin_view(self.ar_aging_view), name="reports_reportcenter_ar_aging"),
            path("payments-report/", self.admin_site.admin_view(self.payments_report_view), name="reports_reportcenter_payments_report"),
        ]
        return custom_urls + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context.update(
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": "Reports",
                "report_links": [
                    {
                        "title": "A/R Aging Report",
                        "description": "Customer balances grouped into current, 1-30, 31-60, 61-90, and 90+ day buckets.",
                        "url": reverse("admin:reports_reportcenter_ar_aging"),
                    },
                    {
                        "title": "Payments Report",
                        "description": "Payment activity by date range with totals and payment methods.",
                        "url": reverse("admin:reports_reportcenter_payments_report"),
                    },
                ],
            }
        )
        return TemplateResponse(request, "admin/reports/reportcenter/change_list.html", extra_context)

    def ar_aging_view(self, request):
        today = timezone.localdate()
        customer_model = Invoice._meta.get_field("customer").related_model
        rows = []
        totals = {
            "current": Decimal("0.00"),
            "days_1_30": Decimal("0.00"),
            "days_31_60": Decimal("0.00"),
            "days_61_90": Decimal("0.00"),
            "days_90_plus": Decimal("0.00"),
            "total": Decimal("0.00"),
        }

        for customer in customer_model.objects.filter(is_active=True).order_by("name", "account_number"):
            invoices = customer.invoices.exclude(status=Invoice.STATUS_VOID).order_by("period_start", "id")
            bucket_totals = {
                "current": Decimal("0.00"),
                "days_1_30": Decimal("0.00"),
                "days_31_60": Decimal("0.00"),
                "days_61_90": Decimal("0.00"),
                "days_90_plus": Decimal("0.00"),
            }
            for invoice in invoices:
                if invoice.issue_date and invoice.issue_date > today:
                    continue
                amount = invoice.total_due or Decimal("0.00")
                if amount <= Decimal("0.00"):
                    continue

                if not invoice.due_date or invoice.due_date >= today:
                    bucket = "current"
                else:
                    days_past_due = (today - invoice.due_date).days
                    if days_past_due <= 30:
                        bucket = "days_1_30"
                    elif days_past_due <= 60:
                        bucket = "days_31_60"
                    elif days_past_due <= 90:
                        bucket = "days_61_90"
                    else:
                        bucket = "days_90_plus"
                bucket_totals[bucket] += amount

            customer_total = sum(bucket_totals.values(), Decimal("0.00"))
            if customer_total <= Decimal("0.00"):
                continue

            for key, value in bucket_totals.items():
                totals[key] += value
            totals["total"] += customer_total

            rows.append(
                {
                    "customer": customer,
                    "current": bucket_totals["current"],
                    "days_1_30": bucket_totals["days_1_30"],
                    "days_31_60": bucket_totals["days_31_60"],
                    "days_61_90": bucket_totals["days_61_90"],
                    "days_90_plus": bucket_totals["days_90_plus"],
                    "total": customer_total,
                    "invoice_url": f'{reverse("admin:billing_invoice_changelist")}?customer__id__exact={customer.pk}',
                    "payment_url": f'{reverse("admin:payments_payment_add")}?customer={customer.pk}',
                }
            )

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "A/R Aging Report",
            "rows": rows,
            "totals": totals,
            "report_date": today,
            "reports_home_url": reverse("admin:reports_reportcenter_changelist"),
        }
        return TemplateResponse(request, "admin/reports/reportcenter/ar_aging.html", context)

    def payments_report_view(self, request):
        today = timezone.localdate()
        date_from = request.GET.get("date_from") or today.replace(day=1).strftime("%Y-%m-%d")
        date_to = request.GET.get("date_to") or today.strftime("%Y-%m-%d")

        payments = Payment.objects.select_related("customer").filter(
            payment_date__gte=date_from,
            payment_date__lte=date_to,
        ).order_by("-payment_date", "-id")

        method_totals = {}
        total_amount = Decimal("0.00")
        for payment in payments:
            label = payment.get_method_display()
            method_totals.setdefault(label, Decimal("0.00"))
            method_totals[label] += payment.amount
            total_amount += payment.amount

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Payments Report",
            "payments": payments,
            "date_from": date_from,
            "date_to": date_to,
            "method_totals": method_totals,
            "total_amount": total_amount,
            "reports_home_url": reverse("admin:reports_reportcenter_changelist"),
        }
        return TemplateResponse(request, "admin/reports/reportcenter/payments_report.html", context)
