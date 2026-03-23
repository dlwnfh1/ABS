import base64
import hashlib
from pathlib import Path
from io import BytesIO

from django.contrib import admin, messages
from django.contrib.admin.views.main import ChangeList
from django.http import HttpResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils import timezone
from xhtml2pdf import pisa

from .models import Invoice, InvoiceItem


class InvoiceChangeList(ChangeList):
    def __init__(self, request, *args, **kwargs):
        self.request = request
        super().__init__(request, *args, **kwargs)

    def get_query_string(self, new_params=None, remove=None):
        query_string = super().get_query_string(new_params=new_params, remove=remove)
        preserved = getattr(self.request, "_custom_filter_params", None)
        if not preserved:
            return query_string

        params = preserved.copy()
        for key in list(params.keys()):
            if key not in self.model_admin.custom_filter_params or not params.get(key):
                params.pop(key, None)

        if not params:
            return query_string

        current_params = params.copy()
        if "?" in query_string:
            for key, values in self.model_admin.request_query_dict(query_string).lists():
                current_params.setlist(key, values)
        return f"?{current_params.urlencode()}" if current_params else "?"


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0
    can_delete = False
    readonly_fields = ("line_type", "description", "period_start", "period_end", "amount")


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    custom_filter_params = {"quick"}
    change_list_template = "admin/billing/invoice/change_list.html"
    change_form_template = "admin/billing/invoice/change_form.html"
    list_per_page = 50
    list_max_show_all = 100000
    list_display = (
        "invoice_number",
        "customer_link",
        "period_start",
        "period_end",
        "issue_date",
        "due_date",
        "partial_payment",
        "subtotal",
        "tax_amount",
        "total_due",
        "status",
        "auto_generated",
    )
    search_fields = ("invoice_number", "customer__account_number", "customer__name")
    date_hierarchy = "period_start"
    inlines = [InvoiceItemInline]
    readonly_fields = ("last_payment_summary", "preview_link")
    list_select_related = ("customer",)

    def get_changelist(self, request, **kwargs):
        return InvoiceChangeList

    @staticmethod
    def request_query_dict(query_string):
        from django.http import QueryDict

        if query_string.startswith("?"):
            query_string = query_string[1:]
        return QueryDict(query_string, mutable=True)

    def get_queryset(self, request):
        filter_params = getattr(request, "_custom_filter_params", request.GET)
        queryset = super().get_queryset(request)
        quick_filter = filter_params.get("quick", "all")
        if quick_filter == "issued":
            queryset = queryset.filter(status=Invoice.STATUS_ISSUED)
        elif quick_filter == "partial":
            queryset = queryset.filter(status=Invoice.STATUS_PARTIAL)
        elif quick_filter == "paid":
            queryset = queryset.filter(status=Invoice.STATUS_PAID)
        elif quick_filter == "auto":
            queryset = queryset.filter(auto_generated=True)
        elif quick_filter == "manual":
            queryset = queryset.filter(auto_generated=False)
        elif quick_filter == "open":
            queryset = queryset.exclude(status__in=[Invoice.STATUS_PAID, Invoice.STATUS_VOID])
        return queryset

    @admin.display(ordering="customer__name", description="Customer")
    def customer_link(self, obj):
        url = reverse("admin:billing_invoice_changelist")
        return format_html(
            '<a href="{}?customer__id__exact={}">{} ({})</a>',
            url,
            obj.customer_id,
            obj.customer.name,
            obj.customer.account_number,
        )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "generator/",
                self.admin_site.admin_view(self.generator_view),
                name="billing_invoice_generator",
            ),
            path(
                "ar-report/",
                self.admin_site.admin_view(self.ar_report_view),
                name="billing_invoice_ar_report",
            ),
            path(
                "<path:object_id>/preview/",
                self.admin_site.admin_view(self.preview_view),
                name="billing_invoice_preview",
            ),
            path(
                "<path:object_id>/pdf/",
                self.admin_site.admin_view(self.pdf_view),
                name="billing_invoice_pdf",
            ),
            path(
                "<path:object_id>/print/",
                self.admin_site.admin_view(self.print_pdf_view),
                name="billing_invoice_print",
            ),
        ]
        return custom_urls + urls

    def changelist_view(self, request, extra_context=None):
        original_get = request.GET.copy()
        request._custom_filter_params = original_get.copy()
        filtered_get = request.GET.copy()
        for key in self.custom_filter_params:
            filtered_get.pop(key, None)
        request.GET = filtered_get
        request.META["QUERY_STRING"] = filtered_get.urlencode()
        extra_context = extra_context or {}
        extra_context["invoice_generator_url"] = reverse("admin:billing_invoice_generator")
        extra_context["ar_report_url"] = reverse("admin:billing_invoice_ar_report")
        extra_context["quick_filters"] = self._build_quick_filters(original_get)
        preserved_filters = original_get.copy()
        for key in list(preserved_filters.keys()):
            if key not in self.custom_filter_params or not preserved_filters.get(key):
                preserved_filters.pop(key, None)
        extra_context["custom_preserved_filters"] = preserved_filters.urlencode()
        show_all_params = original_get.copy()
        show_all_params["all"] = "1"
        paged_params = original_get.copy()
        paged_params.pop("all", None)
        extra_context["show_all_filtered_url"] = f"?{show_all_params.urlencode()}" if show_all_params else "?"
        extra_context["show_paginated_url"] = f"?{paged_params.urlencode()}" if paged_params else "?"
        extra_context["showing_all_filtered"] = original_get.get("all") == "1"
        return super().changelist_view(request, extra_context=extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        extra_context["invoice_preview_url"] = reverse("admin:billing_invoice_preview", args=[object_id])
        extra_context["invoice_pdf_url"] = reverse("admin:billing_invoice_pdf", args=[object_id])
        extra_context["invoice_print_url"] = reverse("admin:billing_invoice_print", args=[object_id])
        return super().change_view(request, object_id, form_url=form_url, extra_context=extra_context)

    def ar_report_view(self, request):
        today = timezone.localdate()
        customer_model = self.model._meta.get_field("customer").related_model
        rows = []
        totals = {
            "current": 0,
            "days_1_30": 0,
            "days_31_60": 0,
            "days_61_90": 0,
            "days_90_plus": 0,
            "total": 0,
        }

        for customer in customer_model.objects.filter(is_active=True).order_by("name", "account_number"):
            invoices = (
                customer.invoices.exclude(status=Invoice.STATUS_VOID)
                .order_by("period_start", "id")
            )
            bucket_totals = {
                "current": 0,
                "days_1_30": 0,
                "days_31_60": 0,
                "days_61_90": 0,
                "days_90_plus": 0,
            }
            for invoice in invoices:
                if invoice.issue_date and invoice.issue_date > today:
                    continue
                amount = invoice.total_due or 0
                if amount <= 0:
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

            customer_total = sum(bucket_totals.values())
            if customer_total <= 0:
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
            "title": "A/R Report",
            "rows": rows,
            "totals": totals,
            "report_date": today,
        }
        return TemplateResponse(request, "admin/billing/invoice/ar_report.html", context)

    def _build_quick_filters(self, params):
        current = params.get("quick", "all")
        options = [
            ("all", "All"),
            ("open", "Open"),
            ("issued", "Issued"),
            ("partial", "Partial"),
            ("paid", "Paid"),
            ("auto", "Auto"),
            ("manual", "Manual"),
        ]
        base_queryset = self.model.objects.all()
        counts = {
            "all": base_queryset.count(),
            "open": base_queryset.exclude(status__in=[Invoice.STATUS_PAID, Invoice.STATUS_VOID]).count(),
            "issued": base_queryset.filter(status=Invoice.STATUS_ISSUED).count(),
            "partial": base_queryset.filter(status=Invoice.STATUS_PARTIAL).count(),
            "paid": base_queryset.filter(status=Invoice.STATUS_PAID).count(),
            "auto": base_queryset.filter(auto_generated=True).count(),
            "manual": base_queryset.filter(auto_generated=False).count(),
        }
        filters = []
        for value, label in options:
            query_params = params.copy()
            if value == "all":
                query_params.pop("quick", None)
            else:
                query_params["quick"] = value
            query_string = query_params.urlencode()
            filters.append(
                {
                    "label": f"{label} ({counts.get(value, 0)})",
                    "url": f"?{query_string}" if query_string else "?",
                    "active": current == value,
                }
            )
        return filters

    @admin.display(description="Preview")
    def preview_link(self, obj):
        if not obj.pk:
            return "-"
        url = reverse("admin:billing_invoice_preview", args=[obj.pk])
        return format_html('<a href="{}" target="_blank">Open invoice preview</a>', url)

    def preview_view(self, request, object_id):
        invoice = self.get_object(request, object_id)
        context = self._invoice_document_context(request, invoice)
        return TemplateResponse(request, "admin/billing/invoice/preview.html", context)

    def pdf_view(self, request, object_id):
        return self._render_pdf_response(request, object_id, as_attachment=True)

    def print_pdf_view(self, request, object_id):
        return self._render_pdf_response(request, object_id, as_attachment=False)

    def _render_pdf_response(self, request, object_id, as_attachment):
        invoice = self.get_object(request, object_id)
        context = self._invoice_document_context(request, invoice)
        html = render_to_string("admin/billing/invoice/pdf.html", context, request=request)
        pdf_buffer = BytesIO()
        self._ensure_md5_compat()
        pdf = pisa.CreatePDF(html, dest=pdf_buffer)
        if pdf.err:
            self.message_user(request, "PDF generation failed.", level=messages.ERROR)
            return redirect("admin:billing_invoice_preview", object_id=object_id)

        response = HttpResponse(pdf_buffer.getvalue(), content_type="application/pdf")
        disposition = "attachment" if as_attachment else "inline"
        response["Content-Disposition"] = f'{disposition}; filename="{invoice.invoice_number}.pdf"'
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

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
    def _logo_symbol_data_uri():
        logo_path = Path(__file__).resolve().parent.parent / "logo_candidate.png"
        if not logo_path.exists():
            return ""
        encoded = base64.b64encode(logo_path.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def _invoice_document_context(self, request, invoice):
        today = timezone.localdate()
        latest_issued_invoice = (
            invoice.customer.invoices.exclude(status=Invoice.STATUS_VOID)
            .filter(issue_date__lte=today)
            .order_by("-period_start", "-id")
            .first()
        )
        items = list(invoice.items.order_by("period_start", "id"))
        padded_items = [
            {
                "description": item.description,
                "period_start": item.period_start,
                "period_end": item.period_end,
                "amount": item.amount,
                "line_type": item.line_type,
            }
            for item in items
        ]
        while len(padded_items) < 4:
            padded_items.append(
                {
                    "description": "",
                    "period_start": None,
                    "period_end": None,
                    "amount": None,
                    "line_type": "",
                }
            )
        padded_items = padded_items[:4]

        return {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": f"Invoice Preview {invoice.invoice_number}",
            "invoice": invoice,
            "items": items,
            "padded_items": padded_items,
            "current_balance_due": invoice.customer.open_balance_as_of(today) if latest_issued_invoice and latest_issued_invoice.pk == invoice.pk else invoice.amount_due_for_allocation(today),
            "preview_date": today,
            "is_latest_issued_invoice": bool(latest_issued_invoice and latest_issued_invoice.pk == invoice.pk),
            "invoice_pdf_url": reverse("admin:billing_invoice_pdf", args=[invoice.pk]),
            "invoice_print_url": reverse("admin:billing_invoice_print", args=[invoice.pk]),
            "pdf_cache_buster": timezone.now().strftime("%Y%m%d%H%M%S%f"),
            "logo_symbol_data_uri": self._logo_symbol_data_uri(),
        }

    def generator_view(self, request):
        candidates = Invoice.get_generation_candidates()
        status_filter = request.GET.get("status", "all")
        query = (request.GET.get("q") or "").strip().lower()
        show_due_only = request.GET.get("due_only") == "1"
        if status_filter != "all":
            candidates = [candidate for candidate in candidates if candidate["status"] == status_filter]
        if show_due_only:
            candidates = [candidate for candidate in candidates if candidate["is_due"]]
        if query:
            candidates = [
                candidate
                for candidate in candidates
                if query in candidate["customer"].name.lower()
                or query in candidate["customer"].account_number.lower()
            ]

        if request.method == "POST":
            selected_ids = request.POST.getlist("customer_ids")
            action = request.POST.get("action", "generate_selected")
            created = 0
            skipped_messages = []
            for candidate in candidates:
                customer = candidate["customer"]
                if str(customer.pk) not in selected_ids:
                    continue
                if action == "generate_all_due":
                    invoices, status, message = Invoice.generate_all_due_for_customer(customer, force=False)
                    if status == "created":
                        created += len(invoices)
                    else:
                        skipped_messages.append(f"{customer.account_number}: {message}")
                elif action == "force_generate_all_due":
                    invoices, status, message = Invoice.generate_all_due_for_customer(customer, force=True)
                    if status == "created":
                        created += len(invoices)
                    else:
                        skipped_messages.append(f"{customer.account_number}: {message}")
                elif action == "force_generate_selected":
                    invoice, status, message = Invoice.generate_for_customer(customer, force=True)
                    if status == "created":
                        created += 1
                    else:
                        skipped_messages.append(f"{customer.account_number}: {message}")
                else:
                    invoice, status, message = Invoice.generate_for_customer(customer, force=False)
                    if status == "created":
                        created += 1
                    else:
                        skipped_messages.append(f"{customer.account_number}: {message}")

            if created:
                self.message_user(request, f"Generated {created} invoice(s).", level=messages.SUCCESS)
            for message in skipped_messages:
                self.message_user(request, message, level=messages.WARNING)
            if not created and not skipped_messages:
                self.message_user(request, "No customers were selected.", level=messages.WARNING)
            return redirect("admin:billing_invoice_generator")

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Invoice Generator",
            "candidates": candidates,
            "status_filter": status_filter,
            "query": request.GET.get("q", ""),
            "show_due_only": show_due_only,
        }
        return TemplateResponse(request, "admin/billing/invoice/generator.html", context)


@admin.register(InvoiceItem)
class InvoiceItemAdmin(admin.ModelAdmin):
    list_display = ("invoice", "line_type", "period_start", "period_end", "amount")
    search_fields = ("invoice__invoice_number", "invoice__customer__account_number", "description")
