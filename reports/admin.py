import csv
import hashlib
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO

from django.contrib import admin
from django.http import HttpResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from xhtml2pdf import pisa

from billing.models import Invoice, add_months
from billing.pdf_utils import list_saved_invoice_pdf_records, merge_saved_invoice_pdf_records
from payments.models import Payment

from .models import DispatchCenter, InvoiceGenerationBatch, ReportCenter, SystemSetting




@admin.register(SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):
    list_display = ("invoice_pdf_output_folder", "payment_check_scan_folder", "updated_at")

    def has_add_permission(self, request):
        return not SystemSetting.objects.exists()

    def changelist_view(self, request, extra_context=None):
        obj = SystemSetting.get_solo()
        if obj:
            return redirect(reverse("admin:reports_systemsetting_change", args=[obj.pk]))
        return redirect(reverse("admin:reports_systemsetting_add"))


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
            path("ar-aging/pdf/", self.admin_site.admin_view(self.ar_aging_pdf_view), name="reports_reportcenter_ar_aging_pdf"),
            path("ar-aging/csv/", self.admin_site.admin_view(self.ar_aging_csv_view), name="reports_reportcenter_ar_aging_csv"),
            path("payments-report/", self.admin_site.admin_view(self.payments_report_view), name="reports_reportcenter_payments_report"),
            path("payments-report/pdf/", self.admin_site.admin_view(self.payments_report_pdf_view), name="reports_reportcenter_payments_report_pdf"),
            path("payments-report/csv/", self.admin_site.admin_view(self.payments_report_csv_view), name="reports_reportcenter_payments_report_csv"),
            path("overdue-customers/", self.admin_site.admin_view(self.overdue_customers_view), name="reports_reportcenter_overdue_customers"),
            path("overdue-customers/pdf/", self.admin_site.admin_view(self.overdue_customers_pdf_view), name="reports_reportcenter_overdue_customers_pdf"),
            path("overdue-customers/csv/", self.admin_site.admin_view(self.overdue_customers_csv_view), name="reports_reportcenter_overdue_customers_csv"),
            path("upcoming-billing/", self.admin_site.admin_view(self.upcoming_billing_view), name="reports_reportcenter_upcoming_billing"),
            path("upcoming-billing/pdf/", self.admin_site.admin_view(self.upcoming_billing_pdf_view), name="reports_reportcenter_upcoming_billing_pdf"),
            path("upcoming-billing/csv/", self.admin_site.admin_view(self.upcoming_billing_csv_view), name="reports_reportcenter_upcoming_billing_csv"),
            path("customer-statement/", self.admin_site.admin_view(self.customer_statement_view), name="reports_reportcenter_customer_statement"),
            path("customer-statement/pdf/", self.admin_site.admin_view(self.customer_statement_pdf_view), name="reports_reportcenter_customer_statement_pdf"),
            path("customer-statement/print/", self.admin_site.admin_view(self.customer_statement_print_view), name="reports_reportcenter_customer_statement_print"),
            path("saved-invoices/", self.admin_site.admin_view(self.saved_invoices_view), name="reports_reportcenter_saved_invoices"),
            path("saved-invoices/merged/pdf/", self.admin_site.admin_view(self.saved_invoices_merged_pdf_view), name="reports_reportcenter_saved_invoices_merged_pdf"),
            path("saved-invoices/merged/print/", self.admin_site.admin_view(self.saved_invoices_merged_print_view), name="reports_reportcenter_saved_invoices_merged_print"),
            path("saved-invoices/batch/toggle-printed/", self.admin_site.admin_view(self.saved_invoices_batch_toggle_printed_view), name="reports_reportcenter_saved_invoices_batch_toggle_printed"),
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
                        "description": "Open receivables grouped by billing term so you can see current balances and customers who are 1, 2, or 3+ terms behind.",
                        "url": reverse("admin:reports_reportcenter_ar_aging"),
                    },
                    {
                        "title": "Payments Report",
                        "description": "Payment activity for a selected date range, including totals by payment method.",
                        "url": reverse("admin:reports_reportcenter_payments_report"),
                    },
                    {
                        "title": "Overdue Customers Report",
                        "description": "Customers with past-due invoices, highest overdue term count, and remaining open balance.",
                        "url": reverse("admin:reports_reportcenter_overdue_customers"),
                    },
                    {
                        "title": "Upcoming Billing Report",
                        "description": "Customers whose next invoice should be issued now or within the next 30 days, grouped for billing planning.",
                        "url": reverse("admin:reports_reportcenter_upcoming_billing"),
                    },
                    {
                        "title": "Customer Statement",
                        "description": "Invoice history, payment history, and current balance for a single customer.",
                        "url": reverse("admin:reports_reportcenter_customer_statement"),
                    },
                ],
            }
        )
        return TemplateResponse(request, "admin/reports/reportcenter/change_list.html", extra_context)

    @staticmethod
    def _parse_optional_iso_date(value, fallback):
        if not value:
            return fallback
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return fallback

    @staticmethod
    def _ensure_md5_compat():
        original_md5 = hashlib.md5

        def md5_compat(*args, **kwargs):
            kwargs.pop("usedforsecurity", None)
            if len(args) > 1:
                args = args[:1]
            return original_md5(*args, **kwargs)

        hashlib.md5 = md5_compat

        module_patches = [
            ("reportlab.pdfbase.pdfdoc", "md5"),
            ("reportlab.pdfbase.cidfonts", "md5"),
            ("reportlab.lib.utils", "md5"),
            ("reportlab.lib.fontfinder", "md5"),
        ]
        for module_name, attr_name in module_patches:
            try:
                module = __import__(module_name, fromlist=[attr_name])
                setattr(module, attr_name, md5_compat)
            except Exception:
                pass

    @staticmethod
    def _add_months(source_date: date, months: int) -> date:
        month_index = source_date.month - 1 + months
        year = source_date.year + month_index // 12
        month = month_index % 12 + 1
        day = min(source_date.day, monthrange(year, month)[1])
        return date(year, month, day)

    def _terms_overdue(self, customer, due_date, today):
        if not due_date or due_date >= today:
            return 0
        term_months = int(customer.billing_term)
        terms = 0
        cursor = due_date
        while cursor < today:
            terms += 1
            cursor = self._add_months(cursor, term_months)
        return terms

    def _pdf_response(self, html, filename, as_attachment):
        pdf_buffer = BytesIO()
        self._ensure_md5_compat()
        pdf = pisa.CreatePDF(html, dest=pdf_buffer)
        if pdf.err:
            return None
        response = HttpResponse(pdf_buffer.getvalue(), content_type="application/pdf")
        disposition = "attachment" if as_attachment else "inline"
        response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

    def _build_ar_aging_data(self):
        today = timezone.localdate()
        customer_model = Invoice._meta.get_field("customer").related_model
        rows = []
        totals = {
            "current": Decimal("0.00"),
            "term_1": Decimal("0.00"),
            "term_2": Decimal("0.00"),
            "term_3_plus": Decimal("0.00"),
            "total": Decimal("0.00"),
        }
        for customer in customer_model.objects.filter(is_active=True).order_by("name", "account_number"):
            invoices = customer.invoices.exclude(status=Invoice.STATUS_VOID).order_by("period_start", "id")
            bucket_totals = {
                "current": Decimal("0.00"),
                "term_1": Decimal("0.00"),
                "term_2": Decimal("0.00"),
                "term_3_plus": Decimal("0.00"),
            }
            for invoice in invoices:
                if invoice.issue_date and invoice.issue_date > today:
                    continue
                amount = invoice.outstanding_amount_as_of(today)
                if amount <= Decimal("0.00"):
                    continue
                terms_overdue = self._terms_overdue(customer, invoice.due_date, today)
                if terms_overdue <= 0:
                    bucket = "current"
                elif terms_overdue == 1:
                    bucket = "term_1"
                elif terms_overdue == 2:
                    bucket = "term_2"
                else:
                    bucket = "term_3_plus"
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
                    "term_1": bucket_totals["term_1"],
                    "term_2": bucket_totals["term_2"],
                    "term_3_plus": bucket_totals["term_3_plus"],
                    "total": customer_total,
                    "invoice_url": f'{reverse("admin:billing_invoice_changelist")}?customer__id__exact={customer.pk}',
                    "payment_url": f'{reverse("admin:payments_payment_add")}?customer={customer.pk}',
                }
            )
        return today, rows, totals

    def _ar_aging_context(self, request):
        report_date, rows, totals = self._build_ar_aging_data()
        return {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "A/R Aging Report",
            "rows": rows,
            "totals": totals,
            "report_date": report_date,
            "reports_home_url": reverse("admin:reports_reportcenter_changelist"),
            "ar_aging_pdf_url": reverse("admin:reports_reportcenter_ar_aging_pdf"),
            "ar_aging_csv_url": reverse("admin:reports_reportcenter_ar_aging_csv"),
        }

    def ar_aging_view(self, request):
        return TemplateResponse(request, "admin/reports/reportcenter/ar_aging.html", self._ar_aging_context(request))

    def ar_aging_pdf_view(self, request):
        context = self._ar_aging_context(request)
        html = render_to_string("admin/reports/reportcenter/ar_aging_pdf.html", context, request=request)
        response = self._pdf_response(html, "ar-aging-report.pdf", as_attachment=True)
        return response or redirect("admin:reports_reportcenter_ar_aging")

    def ar_aging_csv_view(self, request):
        report_date, rows, totals = self._build_ar_aging_data()
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="ar-aging-report.csv"'
        response.write("\ufeff")
        writer = csv.writer(response)
        writer.writerow(["Report Date", report_date.strftime("%m/%d/%Y")])
        writer.writerow([])
        writer.writerow(["Customer", "Account #", "Current", "1 Term Overdue", "2 Terms Overdue", "3+ Terms Overdue", "Total"])
        for row in rows:
            writer.writerow([
                row["customer"].name,
                row["customer"].account_number,
                f'{row["current"]:.2f}',
                f'{row["term_1"]:.2f}',
                f'{row["term_2"]:.2f}',
                f'{row["term_3_plus"]:.2f}',
                f'{row["total"]:.2f}',
            ])
        writer.writerow([])
        writer.writerow(["Totals", "", f'{totals["current"]:.2f}', f'{totals["term_1"]:.2f}', f'{totals["term_2"]:.2f}', f'{totals["term_3_plus"]:.2f}', f'{totals["total"]:.2f}'])
        return response

    def _build_payments_report_data(self, request):
        today = timezone.localdate()
        date_from = request.GET.get("date_from") or today.replace(day=1).strftime("%Y-%m-%d")
        date_to = request.GET.get("date_to") or today.strftime("%Y-%m-%d")
        payments = list(
            Payment.objects.select_related("customer").filter(
                is_voided=False,
                payment_date__gte=date_from,
                payment_date__lte=date_to,
            ).order_by("-payment_date", "-id")
        )
        method_totals = {}
        total_amount = Decimal("0.00")
        for payment in payments:
            label = payment.get_method_display()
            method_totals.setdefault(label, Decimal("0.00"))
            method_totals[label] += payment.amount
            total_amount += payment.amount
        return date_from, date_to, payments, method_totals, total_amount

    def _payments_report_context(self, request):
        date_from, date_to, payments, method_totals, total_amount = self._build_payments_report_data(request)
        query = f'?date_from={date_from}&date_to={date_to}'
        return {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Payments Report",
            "payments": payments,
            "date_from": date_from,
            "date_to": date_to,
            "method_totals": method_totals,
            "total_amount": total_amount,
            "reports_home_url": reverse("admin:reports_reportcenter_changelist"),
            "payments_pdf_url": reverse("admin:reports_reportcenter_payments_report_pdf") + query,
            "payments_csv_url": reverse("admin:reports_reportcenter_payments_report_csv") + query,
            "now": timezone.localdate(),
        }

    def payments_report_view(self, request):
        return TemplateResponse(request, "admin/reports/reportcenter/payments_report.html", self._payments_report_context(request))

    def payments_report_pdf_view(self, request):
        context = self._payments_report_context(request)
        html = render_to_string("admin/reports/reportcenter/payments_report_pdf.html", context, request=request)
        response = self._pdf_response(html, f'payments-report-{context["date_from"]}-to-{context["date_to"]}.pdf', as_attachment=True)
        return response or redirect(f'{reverse("admin:reports_reportcenter_payments_report")}?date_from={context["date_from"]}&date_to={context["date_to"]}')

    def payments_report_csv_view(self, request):
        date_from, date_to, payments, method_totals, total_amount = self._build_payments_report_data(request)
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="payments-report-{date_from}-to-{date_to}.csv"'
        response.write("\ufeff")
        writer = csv.writer(response)
        writer.writerow(["Date From", date_from, "Date To", date_to])
        writer.writerow([])
        writer.writerow(["Date", "Customer", "Account #", "Amount", "Method", "Reference", "Note"])
        for payment in payments:
            writer.writerow([
                payment.payment_date.strftime("%m/%d/%Y"),
                payment.customer.name,
                payment.customer.account_number,
                f'{payment.amount:.2f}',
                payment.get_method_display(),
                payment.reference_number or "",
                payment.note or "",
            ])
        writer.writerow([])
        writer.writerow(["Method Totals"])
        writer.writerow(["Method", "Amount"])
        for method, amount in method_totals.items():
            writer.writerow([method, f'{amount:.2f}'])
        writer.writerow([])
        writer.writerow(["Total Payments", f'{total_amount:.2f}'])
        return response

    def _build_overdue_customers_data(self):
        today = timezone.localdate()
        customer_model = Invoice._meta.get_field("customer").related_model
        rows = []
        totals = {
            "customer_count": 0,
            "overdue_invoices": 0,
            "overdue_total": Decimal("0.00"),
            "open_total": Decimal("0.00"),
            "max_terms_overdue": 0,
        }
        for customer in customer_model.objects.filter(is_active=True).order_by("name", "account_number"):
            overdue_entries = []
            for invoice in customer.invoices.exclude(status=Invoice.STATUS_VOID).order_by("due_date", "period_start", "id"):
                if invoice.issue_date and invoice.issue_date > today:
                    continue
                amount = invoice.outstanding_amount_as_of(today)
                if amount <= Decimal("0.00"):
                    continue
                terms_overdue = self._terms_overdue(customer, invoice.due_date, today)
                if terms_overdue <= 0:
                    continue
                overdue_entries.append((invoice, amount, terms_overdue))
            if not overdue_entries:
                continue
            oldest_invoice = overdue_entries[0][0]
            max_terms_overdue = max(term_count for _, _, term_count in overdue_entries)
            overdue_total = sum((amount for _, amount, _ in overdue_entries), Decimal("0.00"))
            open_total = customer.open_balance_as_of(today)
            rows.append(
                {
                    "customer": customer,
                    "invoice_count": len(overdue_entries),
                    "oldest_due_date": oldest_invoice.due_date,
                    "max_terms_overdue": max_terms_overdue,
                    "overdue_total": overdue_total,
                    "open_total": open_total,
                    "invoice_url": f'{reverse("admin:billing_invoice_changelist")}?customer__id__exact={customer.pk}',
                    "payment_url": f'{reverse("admin:payments_payment_add")}?customer={customer.pk}',
                }
            )
            totals["customer_count"] += 1
            totals["overdue_invoices"] += len(overdue_entries)
            totals["overdue_total"] += overdue_total
            totals["open_total"] += open_total
            totals["max_terms_overdue"] = max(totals["max_terms_overdue"], max_terms_overdue)
        return today, rows, totals

    def _overdue_customers_context(self, request):
        report_date, rows, totals = self._build_overdue_customers_data()
        return {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Overdue Customers Report",
            "rows": rows,
            "totals": totals,
            "report_date": report_date,
            "reports_home_url": reverse("admin:reports_reportcenter_changelist"),
            "overdue_pdf_url": reverse("admin:reports_reportcenter_overdue_customers_pdf"),
            "overdue_csv_url": reverse("admin:reports_reportcenter_overdue_customers_csv"),
        }

    def overdue_customers_view(self, request):
        return TemplateResponse(request, "admin/reports/reportcenter/overdue_customers.html", self._overdue_customers_context(request))

    def overdue_customers_pdf_view(self, request):
        context = self._overdue_customers_context(request)
        html = render_to_string("admin/reports/reportcenter/overdue_customers_pdf.html", context, request=request)
        response = self._pdf_response(html, "overdue-customers-report.pdf", as_attachment=True)
        return response or redirect("admin:reports_reportcenter_overdue_customers")

    def overdue_customers_csv_view(self, request):
        report_date, rows, totals = self._build_overdue_customers_data()
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="overdue-customers-report.csv"'
        response.write("\ufeff")
        writer = csv.writer(response)
        writer.writerow(["Report Date", report_date.strftime("%m/%d/%Y")])
        writer.writerow([])
        writer.writerow(["Customer", "Account #", "Overdue Invoices", "Oldest Due Date", "Max Terms Overdue", "Overdue Total", "Open Balance"])
        for row in rows:
            writer.writerow([
                row["customer"].name,
                row["customer"].account_number,
                row["invoice_count"],
                row["oldest_due_date"].strftime("%m/%d/%Y") if row["oldest_due_date"] else "",
                row["max_terms_overdue"],
                f'{row["overdue_total"]:.2f}',
                f'{row["open_total"]:.2f}',
            ])
        writer.writerow([])
        writer.writerow(["Totals", "", totals["overdue_invoices"], "", totals["max_terms_overdue"], f'{totals["overdue_total"]:.2f}', f'{totals["open_total"]:.2f}'])
        return response

    def _build_upcoming_billing_data(self):
        today = timezone.localdate()
        horizon = today + timedelta(days=30)
        customer_model = Invoice._meta.get_field("customer").related_model
        rows = []
        totals = {
            "ready": 0,
            "due_in_15": 0,
            "due_in_30": 0,
            "total": 0,
            "projected_amount": Decimal("0.00"),
        }
        term_counts = {3: 0, 6: 0, 9: 0, 12: 0}
        term_amounts = {3: Decimal("0.00"), 6: Decimal("0.00"), 9: Decimal("0.00"), 12: Decimal("0.00")}

        for customer in customer_model.objects.filter(is_active=True).order_by("name", "account_number"):
            if not customer.first_billing_date or not customer.services.filter(is_active=True).exists():
                continue

            latest_invoice = customer.invoices.exclude(status=Invoice.STATUS_VOID).order_by("-period_start", "-id").first()
            if latest_invoice:
                period_start = latest_invoice.next_period_start
                period_end = latest_invoice.next_period_end
            else:
                period_start = customer.first_billing_date
                period_end = add_months(period_start, customer.billing_term) - timedelta(days=1)

            issue_date = period_start - timedelta(days=15)
            if issue_date > horizon:
                continue

            existing_invoice = Invoice.objects.filter(customer=customer, period_start=period_start, period_end=period_end).first()
            if existing_invoice:
                continue

            days_until_issue = (issue_date - today).days
            if days_until_issue <= 0:
                status = "Ready"
                status_key = "ready"
            elif days_until_issue <= 15:
                status = "Due in 15 Days"
                status_key = "due_in_15"
            else:
                status = "Due in 30 Days"
                status_key = "due_in_30"

            billing_amount = customer.current_billing_amount
            row = {
                "customer": customer,
                "status": status,
                "issue_date": issue_date,
                "period_start": period_start,
                "period_end": period_end,
                "billing_amount": billing_amount,
                "open_balance": customer.open_balance_as_of(today),
                "billing_term": customer.billing_term,
                "billing_term_label": customer.get_billing_term_display(),
                "invoice_url": f'{reverse("admin:billing_invoice_changelist")}?customer__id__exact={customer.pk}',
                "customer_url": reverse("admin:customers_customer_change", args=[customer.pk]),
            }
            rows.append(row)
            totals[status_key] += 1
            totals["total"] += 1
            totals["projected_amount"] += billing_amount
            term_counts[customer.billing_term] += 1
            term_amounts[customer.billing_term] += billing_amount

        rows.sort(key=lambda row: (row["issue_date"], row["customer"].name, row["customer"].account_number))
        grouped_rows = []
        term_summaries = []
        for term in (3, 6, 9, 12):
            term_rows = [row for row in rows if row["billing_term"] == term]
            if not term_rows:
                continue
            grouped_rows.append(
                {
                    "term": term,
                    "label": f"{term} Month Term",
                    "rows": term_rows,
                    "count": term_counts[term],
                    "projected_amount": term_amounts[term],
                }
            )
            term_summaries.append(
                {
                    "term": term,
                    "label": f"{term} Month Term",
                    "count": term_counts[term],
                    "projected_amount": term_amounts[term],
                }
            )
        return today, horizon, rows, grouped_rows, term_summaries, totals

    def _upcoming_billing_context(self, request):
        report_date, horizon_date, rows, grouped_rows, term_summaries, totals = self._build_upcoming_billing_data()
        return {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Upcoming Billing Report",
            "rows": rows,
            "grouped_rows": grouped_rows,
            "term_summaries": term_summaries,
            "totals": totals,
            "report_date": report_date,
            "horizon_date": horizon_date,
            "reports_home_url": reverse("admin:reports_reportcenter_changelist"),
            "upcoming_pdf_url": reverse("admin:reports_reportcenter_upcoming_billing_pdf"),
            "upcoming_csv_url": reverse("admin:reports_reportcenter_upcoming_billing_csv"),
        }

    def upcoming_billing_view(self, request):
        return TemplateResponse(request, "admin/reports/reportcenter/upcoming_billing.html", self._upcoming_billing_context(request))

    def upcoming_billing_pdf_view(self, request):
        context = self._upcoming_billing_context(request)
        html = render_to_string("admin/reports/reportcenter/upcoming_billing_pdf.html", context, request=request)
        response = self._pdf_response(html, "upcoming-billing-report.pdf", as_attachment=True)
        return response or redirect("admin:reports_reportcenter_upcoming_billing")

    def upcoming_billing_csv_view(self, request):
        report_date, horizon_date, rows, grouped_rows, term_summaries, totals = self._build_upcoming_billing_data()
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="upcoming-billing-report.csv"'
        response.write("\ufeff")
        writer = csv.writer(response)
        writer.writerow(["Report Date", report_date.strftime("%m/%d/%Y"), "Through", horizon_date.strftime("%m/%d/%Y")])
        writer.writerow([])
        writer.writerow(["Summary", "Ready", totals["ready"], "Due in 15 Days", totals["due_in_15"], "Due in 30 Days", totals["due_in_30"], "Projected Amount", f'{totals["projected_amount"]:.2f}'])
        writer.writerow([])
        writer.writerow(["Billing Term", "Customer Count", "Projected Amount"])
        for item in term_summaries:
            writer.writerow([item["label"], item["count"], f'{item["projected_amount"]:.2f}'])
        writer.writerow([])
        writer.writerow(["Customer", "Account #", "Billing Term", "Status", "Next Issue Date", "Billing Period", "Billing Amount", "Open Balance"])
        for row in rows:
            writer.writerow([
                row["customer"].name,
                row["customer"].account_number,
                row["billing_term_label"],
                row["status"],
                row["issue_date"].strftime("%m/%d/%Y"),
                f'{row["period_start"].strftime("%m/%d/%Y")} - {row["period_end"].strftime("%m/%d/%Y")}',
                f'{row["billing_amount"]:.2f}',
                f'{row["open_balance"]:.2f}',
            ])
        return response

    def customer_statement_view(self, request):
        customer_model = Invoice._meta.get_field("customer").related_model
        customers = customer_model.objects.order_by("name", "account_number")
        customer_id = request.GET.get("customer")
        selected_customer = None
        invoices = []
        payments = []
        open_balance = Decimal("0.00")
        last_payment = None
        if customer_id:
            selected_customer = customers.filter(pk=customer_id).first()
            if selected_customer:
                today = timezone.localdate()
                invoices = list(selected_customer.invoices.exclude(status=Invoice.STATUS_VOID).order_by("-period_start", "-id"))
                payments = list(
                    selected_customer.payments.filter(is_voided=False)
                    .prefetch_related("allocations__invoice")
                    .order_by("-payment_date", "-id")
                )
                open_balance = selected_customer.open_balance_as_of(today)
                last_payment = payments[0] if payments else None
        context = self._customer_statement_context(request, customers, customer_id, selected_customer, invoices, payments, open_balance, last_payment)
        return TemplateResponse(request, "admin/reports/reportcenter/customer_statement.html", context)

    def _customer_statement_context(self, request, customers, customer_id, selected_customer, invoices, payments, open_balance, last_payment):
        pdf_qs = f'?customer={selected_customer.pk}' if selected_customer else ''
        return {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Customer Statement",
            "customers": customers,
            "selected_customer": selected_customer,
            "selected_customer_id": int(customer_id) if customer_id and customer_id.isdigit() else None,
            "invoices": invoices,
            "payments": payments,
            "open_balance": open_balance,
            "last_payment": last_payment,
            "reports_home_url": reverse("admin:reports_reportcenter_changelist"),
            "statement_pdf_url": reverse("admin:reports_reportcenter_customer_statement_pdf") + pdf_qs,
            "statement_print_url": reverse("admin:reports_reportcenter_customer_statement_print") + pdf_qs,
            "now": timezone.localdate(),
        }

    def customer_statement_pdf_view(self, request):
        return self._render_customer_statement_pdf(request, as_attachment=True)

    def customer_statement_print_view(self, request):
        return self._render_customer_statement_pdf(request, as_attachment=False)

    def _render_customer_statement_pdf(self, request, as_attachment):
        customer_model = Invoice._meta.get_field("customer").related_model
        customers = customer_model.objects.order_by("name", "account_number")
        customer_id = request.GET.get("customer")
        selected_customer = customers.filter(pk=customer_id).first() if customer_id else None
        invoices = []
        payments = []
        open_balance = Decimal("0.00")
        last_payment = None
        if selected_customer:
            today = timezone.localdate()
            invoices = list(selected_customer.invoices.exclude(status=Invoice.STATUS_VOID).order_by("-period_start", "-id"))
            payments = list(
                selected_customer.payments.filter(is_voided=False)
                .prefetch_related("allocations__invoice")
                .order_by("-payment_date", "-id")
            )
            open_balance = selected_customer.open_balance_as_of(today)
            last_payment = payments[0] if payments else None
        context = self._customer_statement_context(request, customers, customer_id, selected_customer, invoices, payments, open_balance, last_payment)
        if not selected_customer:
            return redirect("admin:reports_reportcenter_customer_statement")
        html = render_to_string("admin/reports/reportcenter/customer_statement_pdf.html", context, request=request)
        response = self._pdf_response(html, f"statement-{selected_customer.account_number}.pdf", as_attachment=as_attachment)
        if response is None:
            return redirect(f'{reverse("admin:reports_reportcenter_customer_statement")}?customer={selected_customer.pk}')
        return response

    def saved_invoices_view(self, request):
        printed_scope = (request.GET.get("printed_scope") or "unprinted").strip().lower()
        if printed_scope not in {"unprinted", "all", "printed"}:
            printed_scope = "unprinted"
        result = list_saved_invoice_pdf_records(
            limit=500,
            marker="CURRENT",
            printed_scope=printed_scope,
        )
        records = self._prepare_dispatch_records(result["records"])
        query_string = request.GET.copy()
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Invoice Dispatch",
            "records": records,
            "base_folder": result["base_folder"],
            "printed_scope": printed_scope,
            "dispatch_home_url": reverse("admin:reports_dispatchcenter_changelist"),
            "merged_pdf_url": f'{reverse("admin:reports_reportcenter_saved_invoices_merged_pdf")}?{query_string.urlencode()}',
            "merged_print_url": f'{reverse("admin:reports_reportcenter_saved_invoices_merged_print")}?{query_string.urlencode()}',
        }
        return TemplateResponse(request, "admin/reports/reportcenter/saved_invoices.html", context)

    def _saved_invoice_filtered_records(self, request):
        printed_scope = (request.GET.get("printed_scope") or "unprinted").strip().lower()
        if printed_scope not in {"unprinted", "all", "printed"}:
            printed_scope = "unprinted"
        return list_saved_invoice_pdf_records(
            limit=0,
            marker="CURRENT",
            printed_scope=printed_scope,
        )["records"]

    def _saved_invoice_selected_batch(self, request):
        batch_id = (request.GET.get("batch_id") or "").strip()
        if batch_id == "latest":
            return list_saved_invoice_pdf_records(limit=1)["latest_batch"]
        if batch_id.isdigit():
            return InvoiceGenerationBatch.objects.filter(pk=int(batch_id)).first()
        return None

    def _mark_visible_batches_printed(self, records):
        batch_ids = sorted({record["batch_id"] for record in records if record.get("batch_id")})
        if not batch_ids:
            return []
        batches = list(InvoiceGenerationBatch.objects.filter(pk__in=batch_ids))
        now = timezone.now()
        updated = []
        for batch in batches:
            if batch.is_printed:
                continue
            batch.is_printed = True
            batch.printed_at = now
            batch.save(update_fields=["is_printed", "printed_at"])
            updated.append(batch)
        return updated

    def _prepare_dispatch_records(self, records):
        seen_batches = set()
        prepared = []
        shade_index = 0
        last_batch_id = object()
        for record in records:
            item = dict(record)
            batch_id = item.get("batch_id")
            is_new_batch = batch_id != last_batch_id
            if is_new_batch:
                shade_index += 1
            item["show_batch_toggle"] = bool(batch_id) and batch_id not in seen_batches
            item["batch_group_start"] = is_new_batch
            item["batch_group_class"] = f"batch-shade-{1 if shade_index % 2 else 2}"
            if batch_id:
                seen_batches.add(batch_id)
            last_batch_id = batch_id
            prepared.append(item)
        return prepared

    def saved_invoices_merged_pdf_view(self, request):
        records = self._saved_invoice_filtered_records(request)
        pdf_bytes = merge_saved_invoice_pdf_records(records)
        if not pdf_bytes:
            self.message_user(request, "No saved invoice PDFs matched the current filter.", level=40)
            return redirect(f'{reverse("admin:reports_reportcenter_saved_invoices")}?{request.GET.urlencode()}')
        updated_batches = self._mark_visible_batches_printed(records)
        if len(updated_batches) == 1:
            self.message_user(request, f"{updated_batches[0].label} was marked as printed.")
        elif updated_batches:
            self.message_user(request, f"{len(updated_batches)} batches were marked as printed.")
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = 'attachment; filename="saved-invoices-merged.pdf"'
        return response

    def saved_invoices_merged_print_view(self, request):
        records = self._saved_invoice_filtered_records(request)
        pdf_bytes = merge_saved_invoice_pdf_records(records)
        if not pdf_bytes:
            self.message_user(request, "No saved invoice PDFs matched the current filter.", level=40)
            return redirect(f'{reverse("admin:reports_reportcenter_saved_invoices")}?{request.GET.urlencode()}')
        updated_batches = self._mark_visible_batches_printed(records)
        if len(updated_batches) == 1:
            self.message_user(request, f"{updated_batches[0].label} was marked as printed.")
        elif updated_batches:
            self.message_user(request, f"{len(updated_batches)} batches were marked as printed.")
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = 'inline; filename="saved-invoices-merged.pdf"'
        return response

    def saved_invoices_batch_toggle_printed_view(self, request):
        batch_id = request.POST.get("batch_id")
        query_string = request.POST.get("return_query", "")
        if not batch_id or not batch_id.isdigit():
            self.message_user(request, "Choose a valid batch first.", level=40)
            return redirect(f'{reverse("admin:reports_reportcenter_saved_invoices")}?{query_string}' if query_string else reverse("admin:reports_reportcenter_saved_invoices"))
        batch = InvoiceGenerationBatch.objects.filter(pk=int(batch_id)).first()
        if not batch:
            self.message_user(request, "Batch not found.", level=40)
            return redirect(f'{reverse("admin:reports_reportcenter_saved_invoices")}?{query_string}' if query_string else reverse("admin:reports_reportcenter_saved_invoices"))
        batch.is_printed = not batch.is_printed
        batch.printed_at = timezone.now() if batch.is_printed else None
        batch.save(update_fields=["is_printed", "printed_at"])
        if batch.is_printed:
            self.message_user(request, f"{batch.label} was marked as printed.")
        else:
            self.message_user(request, f"{batch.label} printed status was cleared.")
        return redirect(f'{reverse("admin:reports_reportcenter_saved_invoices")}?{query_string}' if query_string else reverse("admin:reports_reportcenter_saved_invoices"))


@admin.register(DispatchCenter)
class DispatchCenterAdmin(admin.ModelAdmin):
    change_list_template = "admin/reports/dispatchcenter/change_list.html"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return DispatchCenter.objects.none()

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context.update(
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": "Invoice Dispatch",
                "dispatch_links": [
                    {
                        "title": "Invoice Dispatch",
                        "description": "Open saved invoice files, filter unprinted batches, and print visible invoices as one PDF.",
                        "url": reverse("admin:reports_reportcenter_saved_invoices"),
                    },
                ],
            }
        )
        return TemplateResponse(request, "admin/reports/dispatchcenter/change_list.html", extra_context)
