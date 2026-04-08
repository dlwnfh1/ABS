from datetime import datetime, timedelta
from django import forms
from django.forms.models import BaseInlineFormSet
import csv
from decimal import Decimal, InvalidOperation

from django.contrib import admin
from django.contrib import messages
from django.contrib.admin.views.main import ChangeList
from django.db.models import Count, Max, Prefetch, Q
from django.http import HttpResponseRedirect, HttpResponse
from django.db import transaction
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils import timezone
from billing.models import Invoice
from billing.pdf_utils import save_invoices_to_configured_folder

from .models import Customer, Service


class CustomerChangeList(ChangeList):
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



class ServiceForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        customer = None
        if self.instance and getattr(self.instance, "customer_id", None):
            customer = self.instance.customer
        elif self.initial.get("customer"):
            customer = self.initial.get("customer")
        if not self.instance.pk:
            self.fields["service_name"].initial = self.initial.get("service_name") or "Alarm Monitoring Service"
            if customer:
                self.fields["service_address1"].initial = self.initial.get("service_address1") or customer.billing_address1
                self.fields["service_address2"].initial = self.initial.get("service_address2") or customer.billing_address2


class ServiceInlineFormSet(BaseInlineFormSet):
    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        if not form.instance.pk:
            form.fields["service_name"].initial = form.fields["service_name"].initial or "Alarm Monitoring Service"
            if self.instance and getattr(self.instance, "pk", None):
                form.fields["service_address1"].initial = form.fields["service_address1"].initial or self.instance.billing_address1
                form.fields["service_address2"].initial = form.fields["service_address2"].initial or self.instance.billing_address2
        return form

class ServiceInline(admin.TabularInline):
    model = Service
    form = ServiceForm
    formset = ServiceInlineFormSet
    extra = 0


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    custom_filter_params = {"workflow_status", "active_state", "term"}
    change_list_template = "admin/customers/customer/change_list.html"
    change_form_template = "admin/customers/customer/change_form.html"
    list_per_page = 25
    list_max_show_all = 100000
    list_display = (
        "account_number",
        "name",
        "next_invoice_status",
        "next_invoice_period",
        "open_balance",
        "last_payment_date_display",
        "view_invoices_link",
        "payment_count",
        "payment_actions",
        "is_active",
    )
    search_fields = (
        "account_number",
        "name",
        "email_address",
        "billing_address1",
        "billing_address2",
        "services__service_address1",
        "services__service_address2",
    )
    inlines = [ServiceInline]
    actions = [
        "generate_all_due_action",
        "force_generate_all_due_action",
        "generate_next_action",
        "force_generate_next_action",
    ]

    def _ensure_candidate_map(self):
        if hasattr(self, "_candidate_map"):
            return
        queryset = self.model.objects.annotate(
            invoice_total=Count("invoices", distinct=True),
            payment_total=Count("payments", distinct=True),
            last_payment_date=Max("payments__payment_date", filter=Q(payments__is_voided=False)),
        ).prefetch_related(
            Prefetch(
                "invoices",
                queryset=Invoice.objects.exclude(status=Invoice.STATUS_VOID).order_by("-period_start", "-id"),
                to_attr="prefetched_invoices",
            )
        )
        self._candidate_map = {
            customer.pk: self._build_customer_workflow(customer)
            for customer in queryset
        }

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "import-csv/",
                self.admin_site.admin_view(self.import_csv_view),
                name="customers_customer_import_csv",
            ),
            path(
                "export-csv/",
                self.admin_site.admin_view(self.export_csv_view),
                name="customers_customer_export_csv",
            ),
            path(
                "<path:object_id>/generate-next/",
                self.admin_site.admin_view(self.generate_next_invoice_view),
                name="customers_customer_generate_next",
            ),
            path(
                "<path:object_id>/force-generate-next/",
                self.admin_site.admin_view(self.force_generate_next_invoice_view),
                name="customers_customer_force_generate_next",
            ),
            path(
                "<path:object_id>/generate-all-due/",
                self.admin_site.admin_view(self.generate_all_due_invoices_view),
                name="customers_customer_generate_all_due",
            ),
            path(
                "<path:object_id>/force-generate-all-due/",
                self.admin_site.admin_view(self.force_generate_all_due_invoices_view),
                name="customers_customer_force_generate_all_due",
            ),
        ]
        return custom_urls + urls

    def get_changelist(self, request, **kwargs):
        return CustomerChangeList

    @staticmethod
    def request_query_dict(query_string):
        from django.http import QueryDict

        if query_string.startswith("?"):
            query_string = query_string[1:]
        return QueryDict(query_string, mutable=True)

    def get_queryset(self, request):
        filter_params = getattr(request, "_custom_filter_params", request.GET)
        queryset = super().get_queryset(request)
        queryset = queryset.annotate(
            invoice_total=Count("invoices", distinct=True),
            payment_total=Count("payments", distinct=True),
            last_payment_date=Max("payments__payment_date", filter=Q(payments__is_voided=False)),
        ).prefetch_related(
            Prefetch(
                "invoices",
                queryset=Invoice.objects.exclude(status=Invoice.STATUS_VOID).order_by("-period_start", "-id"),
                to_attr="prefetched_invoices",
            )
        )
        self._candidate_map = {
            customer.pk: self._build_customer_workflow(customer)
            for customer in queryset
        }
        status_filter = filter_params.get("workflow_status", "all")
        active_filter = filter_params.get("active_state", "all")
        term_filter = filter_params.get("term", "all")

        if active_filter == "active":
            queryset = queryset.filter(is_active=True)
        elif active_filter == "inactive":
            queryset = queryset.filter(is_active=False)

        if term_filter in {"3", "6", "9", "12"}:
            queryset = queryset.filter(billing_term=int(term_filter))

        if status_filter != "all":
            customer_ids = [
                customer_id
                for customer_id, candidate in self._candidate_map.items()
                if candidate["status"] == status_filter
            ]
            queryset = queryset.filter(pk__in=customer_ids)

        return queryset

    def changelist_view(self, request, extra_context=None):
        original_get = request.GET.copy()
        request._custom_filter_params = original_get.copy()
        filtered_get = request.GET.copy()
        for key in self.custom_filter_params:
            filtered_get.pop(key, None)
        request.GET = filtered_get
        request.META["QUERY_STRING"] = filtered_get.urlencode()
        self._ensure_candidate_map()
        extra_context = extra_context or {}
        extra_context["workflow_filters"] = self._build_workflow_filters(original_get)
        extra_context["active_filters"] = self._build_active_filters(original_get)
        extra_context["term_filters"] = self._build_term_filters(original_get)
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
        extra_context["import_csv_url"] = reverse("admin:customers_customer_import_csv")
        extra_context["export_csv_url"] = reverse("admin:customers_customer_export_csv")
        return super().changelist_view(request, extra_context=extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        customer = self.get_object(request, object_id)
        extra_context["generate_next_url"] = reverse("admin:customers_customer_generate_next", args=[object_id])
        extra_context["force_generate_next_url"] = reverse("admin:customers_customer_force_generate_next", args=[object_id])
        extra_context["generate_all_due_url"] = reverse("admin:customers_customer_generate_all_due", args=[object_id])
        extra_context["force_generate_all_due_url"] = reverse("admin:customers_customer_force_generate_all_due", args=[object_id])
        extra_context["add_payment_url"] = f'{reverse("admin:payments_payment_quick_entry")}?customer={object_id}'
        extra_context["view_payments_url"] = f'{reverse("admin:payments_payment_changelist")}?customer__id__exact={object_id}'
        extra_context["summary_cards"] = self._build_summary_cards(customer)
        return super().change_view(request, object_id, form_url=form_url, extra_context=extra_context)

    def _customer_open_balance(self, customer, as_of_date=None):
        as_of_date = as_of_date or timezone.localdate()
        return customer.open_balance_as_of(as_of_date)

    def _build_summary_cards(self, customer):
        candidate = self._candidate_for(customer)
        latest_invoice = self._latest_invoice(customer)
        today = timezone.localdate()
        open_balance = self._customer_open_balance(customer, as_of_date=today)

        last_payment = customer.payments.filter(is_voided=False).order_by("-payment_date", "-id").first()
        last_voided_payment = customer.payments.filter(is_voided=True).order_by("-voided_at", "-id").first()
        status_label = self.next_invoice_status(customer)
        if hasattr(status_label, "__html__"):
            status_label = str(status_label)

        return [
            {
                "label": "Open Balance",
                "value": f"${open_balance.quantize(Decimal('0.01')):.2f}",
                "tone": "alert" if open_balance > Decimal("0.00") else "normal",
            },
            {
                "label": "Next Status",
                "value": status_label,
                "tone": "normal",
                "safe": True,
            },
            {
                "label": "Next Billing Period",
                "value": self.next_invoice_period(customer),
                "tone": "normal",
            },
            {
                "label": "Next Issue Date",
                "value": self.next_issue_date(customer),
                "tone": "normal",
            },
            {
                "label": "Current Billing Amount",
                "value": f"${customer.current_billing_amount:.2f}",
                "tone": "normal",
            },
            {
                "label": "Last Payment",
                "value": last_payment.payment_date.strftime("%m/%d/%Y") if last_payment else "-",
                "tone": "normal",
            },
            {
                "label": "Last Payment Amount",
                "value": f"${last_payment.amount:.2f}" if last_payment else "-",
                "tone": "normal",
            },
            {
                "label": "Last Payment Method",
                "value": last_payment.get_method_display() if last_payment else "-",
                "tone": "normal",
            },
            {
                "label": "Last Voided Payment",
                "value": (
                    f"${last_voided_payment.amount:.2f} on {last_voided_payment.payment_date:%m/%d/%Y}"
                    if last_voided_payment else "-"
                ),
                "tone": "normal",
            },
            {
                "label": "Voided On",
                "value": (
                    last_voided_payment.voided_at.astimezone(timezone.get_current_timezone()).strftime("%m/%d/%Y %I:%M %p")
                    if last_voided_payment and last_voided_payment.voided_at else "-"
                ),
                "tone": "normal",
            },
            {
                "label": "Latest Invoice",
                "value": latest_invoice.invoice_number if latest_invoice else "-",
                "tone": "normal",
            },
        ]

    def generate_next_invoice_view(self, request, object_id):
        customer = self.get_object(request, object_id)
        invoice, status, message = Invoice.generate_for_customer(customer, force=False)
        level = messages.SUCCESS if status == "created" else messages.WARNING
        self.message_user(request, f"{customer.account_number}: {message}", level=level)
        if status == "created" and invoice:
            save_result = save_invoices_to_configured_folder([invoice], created_by=request.user.get_username())
            if save_result and save_result.get("saved_count"):
                self.message_user(request, f'Saved {save_result["saved_count"]} invoice PDF(s) to {save_result["date_folder"]}. Batch: {save_result["batch_label"]}.', level=messages.SUCCESS)
        return HttpResponseRedirect(reverse("admin:customers_customer_change", args=[object_id]))

    def force_generate_next_invoice_view(self, request, object_id):
        customer = self.get_object(request, object_id)
        invoice, status, message = Invoice.generate_for_customer(customer, force=True)
        level = messages.SUCCESS if status == "created" else messages.WARNING
        self.message_user(request, f"{customer.account_number}: {message}", level=level)
        if status == "created" and invoice:
            save_result = save_invoices_to_configured_folder([invoice], created_by=request.user.get_username())
            if save_result and save_result.get("saved_count"):
                self.message_user(request, f'Saved {save_result["saved_count"]} invoice PDF(s) to {save_result["date_folder"]}. Batch: {save_result["batch_label"]}.', level=messages.SUCCESS)
        return HttpResponseRedirect(reverse("admin:customers_customer_change", args=[object_id]))

    def generate_all_due_invoices_view(self, request, object_id):
        customer = self.get_object(request, object_id)
        invoices, status, message = Invoice.generate_all_due_for_customer(customer, force=False)
        level = messages.SUCCESS if status == "created" else messages.WARNING
        self.message_user(request, f"{customer.account_number}: {message}", level=level)
        if status == "created" and invoices:
            save_result = save_invoices_to_configured_folder(invoices, created_by=request.user.get_username())
            if save_result and save_result.get("saved_count"):
                self.message_user(request, f'Saved {save_result["saved_count"]} invoice PDF(s) to {save_result["date_folder"]}. Batch: {save_result["batch_label"]}.', level=messages.SUCCESS)
        return HttpResponseRedirect(reverse("admin:customers_customer_change", args=[object_id]))

    def force_generate_all_due_invoices_view(self, request, object_id):
        customer = self.get_object(request, object_id)
        invoices, status, message = Invoice.generate_all_due_for_customer(customer, force=True)
        level = messages.SUCCESS if status == "created" else messages.WARNING
        self.message_user(request, f"{customer.account_number}: {message}", level=level)
        if status == "created" and invoices:
            save_result = save_invoices_to_configured_folder(invoices, created_by=request.user.get_username())
            if save_result and save_result.get("saved_count"):
                self.message_user(request, f'Saved {save_result["saved_count"]} invoice PDF(s) to {save_result["date_folder"]}. Batch: {save_result["batch_label"]}.', level=messages.SUCCESS)
        return HttpResponseRedirect(reverse("admin:customers_customer_change", args=[object_id]))

    @admin.action(description="Generate All Due for selected customers")
    def generate_all_due_action(self, request, queryset):
        return self._run_invoice_action(request, queryset, mode="all_due", force=False)

    @admin.action(description="Force Generate All Due for selected customers")
    def force_generate_all_due_action(self, request, queryset):
        return self._run_invoice_action(request, queryset, mode="all_due", force=True)

    @admin.action(description="Generate Next Invoice for selected customers")
    def generate_next_action(self, request, queryset):
        return self._run_invoice_action(request, queryset, mode="next", force=False)

    @admin.action(description="Force Generate Next Invoice for selected customers")
    def force_generate_next_action(self, request, queryset):
        return self._run_invoice_action(request, queryset, mode="next", force=True)

    def _run_invoice_action(self, request, queryset, mode, force):
        created = 0
        created_invoice_ids = []
        skipped_messages = []
        for customer in queryset:
            if mode == "all_due":
                invoices, status, message = Invoice.generate_all_due_for_customer(customer, force=force)
                if status == "created":
                    created += len(invoices)
                    created_invoice_ids.extend(str(invoice.pk) for invoice in invoices)
                else:
                    skipped_messages.append(f"{customer.account_number}: {message}")
            else:
                invoice, status, message = Invoice.generate_for_customer(customer, force=force)
                if status == "created":
                    created += 1
                    created_invoice_ids.append(str(invoice.pk))
                else:
                    skipped_messages.append(f"{customer.account_number}: {message}")

        save_result = None
        if created_invoice_ids:
            created_invoices = list(Invoice.objects.filter(pk__in=created_invoice_ids).select_related("customer"))
            save_result = save_invoices_to_configured_folder(created_invoices, created_by=request.user.get_username())
        if created:
            self.message_user(request, f"Generated {created} invoice(s).", level=messages.SUCCESS)
        if save_result and save_result.get("saved_count"):
            self.message_user(request, f'Saved {save_result["saved_count"]} invoice PDF(s) to {save_result["date_folder"]}. Batch: {save_result["batch_label"]}.', level=messages.SUCCESS)
        for message in skipped_messages[:10]:
            self.message_user(request, message, level=messages.WARNING)
        if len(skipped_messages) > 10:
            self.message_user(request, f"{len(skipped_messages) - 10} more customers were skipped.", level=messages.WARNING)
        if created_invoice_ids:
            invoice_list_url = reverse("admin:billing_invoice_changelist")
            return HttpResponseRedirect(f'{invoice_list_url}?generated_ids={",".join(created_invoice_ids)}')

    def export_csv_view(self, request):
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="customers_export.csv"'
        response.write("\ufeff")
        writer = csv.writer(response)
        writer.writerow(
            [
                "account_number",
                "customer_name",
                "billing_address1",
                "billing_address2",
                "email_address",
                "billing_term",
                "tax_rate",
                "first_billing_date",
                "customer_is_active",
                "service_name",
                "service_address1",
                "service_address2",
                "activation_date",
                "billing_amount",
                "service_is_active",
            ]
        )
        customers = (
            Customer.objects.prefetch_related("services")
            .order_by("account_number", "services__service_name", "services__id")
        )
        for customer in customers:
            services = list(customer.services.all()) or [None]
            for service in services:
                writer.writerow(
                    [
                        customer.account_number,
                        customer.name,
                        customer.billing_address1,
                        customer.billing_address2,
                        customer.email_address,
                        customer.billing_term,
                        customer.tax_rate,
                        customer.first_billing_date.strftime("%m-%d-%Y") if customer.first_billing_date else "",
                        "1" if customer.is_active else "0",
                        service.service_name if service else "",
                        service.service_address1 if service else "",
                        service.service_address2 if service else "",
                        service.activation_date.strftime("%m-%d-%Y") if service and service.activation_date else "",
                        service.billing_amount if service else "",
                        "1" if service and service.is_active else "0",
                    ]
                )
        return response

    def import_csv_view(self, request):
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Import Customers CSV",
            "expected_columns": [
                "account_number",
                "customer_name",
                "billing_address1",
                "billing_address2",
                "email_address",
                "billing_term",
                "tax_rate",
                "first_billing_date",
                "customer_is_active",
                "service_name",
                "service_address1",
                "service_address2",
                "activation_date",
                "billing_amount",
                "service_is_active",
            ],
        }
        if request.method == "POST":
            upload = request.FILES.get("csv_file")
            if not upload:
                self.message_user(request, "Select a CSV file to import.", level=messages.ERROR)
                return TemplateResponse(request, "admin/customers/customer/import_csv.html", context)
            try:
                created_customers, updated_customers, created_services, updated_services = self._import_csv_file(upload)
            except ValueError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
                return TemplateResponse(request, "admin/customers/customer/import_csv.html", context)
            self.message_user(
                request,
                (
                    f"Imported customers successfully. "
                    f"Customers created: {created_customers}, updated: {updated_customers}. "
                    f"Services created: {created_services}, updated: {updated_services}."
                ),
                level=messages.SUCCESS,
            )
            return HttpResponseRedirect(reverse("admin:customers_customer_changelist"))
        return TemplateResponse(request, "admin/customers/customer/import_csv.html", context)

    def _import_csv_file(self, upload):
        raw_bytes = upload.read()
        try:
            decoded_text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                decoded_text = raw_bytes.decode("cp949")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    "CSV encoding must be UTF-8, UTF-8 with BOM, or CP949."
                ) from exc
        decoded_lines = decoded_text.splitlines()
        reader = csv.DictReader(decoded_lines)
        required_columns = {
            "account_number",
            "customer_name",
            "billing_address1",
            "billing_term",
            "tax_rate",
            "service_name",
            "service_address1",
            "billing_amount",
        }
        if not reader.fieldnames:
            raise ValueError("The CSV file is empty.")
        missing = required_columns.difference(reader.fieldnames)
        if missing:
            raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")

        created_customers = 0
        updated_customers = 0
        created_services = 0
        updated_services = 0

        with transaction.atomic():
            for index, row in enumerate(reader, start=2):
                account_number = (row.get("account_number") or "").strip()
                customer_name = (row.get("customer_name") or "").strip()
                billing_address1 = (row.get("billing_address1") or "").strip()
                if not account_number or not customer_name or not billing_address1:
                    raise ValueError(f"Row {index}: account_number, customer_name, and billing_address1 are required.")

                billing_term = self._parse_int(row.get("billing_term"), "billing_term", index)
                if billing_term not in {3, 6, 9, 12}:
                    raise ValueError(f"Row {index}: billing_term must be one of 3, 6, 9, or 12.")

                customer_defaults = {
                    "name": customer_name,
                    "billing_address1": billing_address1,
                    "billing_address2": (row.get("billing_address2") or "").strip(),
                    "email_address": (row.get("email_address") or "").strip(),
                    "billing_term": billing_term,
                    "tax_rate": self._parse_decimal(row.get("tax_rate"), "tax_rate", index),
                    "first_billing_date": self._parse_optional_date(row.get("first_billing_date"), index),
                    "is_active": self._parse_bool(row.get("customer_is_active"), default=True),
                }
                customer, created = Customer.objects.update_or_create(
                    account_number=account_number,
                    defaults=customer_defaults,
                )
                if created:
                    created_customers += 1
                else:
                    updated_customers += 1

                service_name = (row.get("service_name") or "").strip()
                service_address1 = (row.get("service_address1") or "").strip()
                if not service_name or not service_address1:
                    raise ValueError(f"Row {index}: service_name and service_address1 are required.")

                service_defaults = {
                    "service_address2": (row.get("service_address2") or "").strip(),
                    "activation_date": self._parse_optional_service_date(row.get("activation_date"), "activation_date", index),
                    "billing_amount": self._parse_decimal(row.get("billing_amount"), "billing_amount", index),
                    "is_active": self._parse_bool(row.get("service_is_active"), default=True),
                }
                service, service_created = Service.objects.update_or_create(
                    customer=customer,
                    service_name=service_name,
                    service_address1=service_address1,
                    defaults=service_defaults,
                )
                if service_created:
                    created_services += 1
                else:
                    updated_services += 1

        return created_customers, updated_customers, created_services, updated_services

    def _parse_decimal(self, value, field_name, row_number):
        try:
            return Decimal(str(value).strip())
        except (InvalidOperation, AttributeError):
            raise ValueError(f"Row {row_number}: {field_name} must be a valid decimal number.")

    def _parse_int(self, value, field_name, row_number):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError, AttributeError):
            raise ValueError(f"Row {row_number}: {field_name} must be a valid integer.")

    def _parse_required_date(self, value, field_name, row_number):
        parsed = self._parse_csv_date((value or "").strip())
        if not parsed:
            raise ValueError(f"Row {row_number}: {field_name} must be in MM-DD-YYYY or M/D/YYYY format.")
        return parsed

    def _parse_optional_service_date(self, value, field_name, row_number):
        raw = (value or "").strip()
        if not raw:
            return None
        parsed = self._parse_csv_date(raw)
        if not parsed:
            raise ValueError(f"Row {row_number}: {field_name} must be in MM-DD-YYYY or M/D/YYYY format.")
        return parsed

    def _parse_optional_date(self, value, row_number):
        raw = (value or "").strip()
        if not raw:
            return None
        parsed = self._parse_csv_date(raw)
        if not parsed:
            raise ValueError(f"Row {row_number}: first_billing_date must be in MM-DD-YYYY or M/D/YYYY format.")
        return parsed

    def _parse_csv_date(self, value):
        for fmt in ("%m-%d-%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        return None

    def _parse_bool(self, value, default=True):
        raw = (str(value).strip().lower() if value is not None else "")
        if not raw:
            return default
        return raw in {"1", "true", "yes", "y"}

    def _replace_query(self, params, **updates):
        params = params.copy()
        for key, value in updates.items():
            if value in (None, "", "all"):
                params.pop(key, None)
            else:
                params[key] = value
        query_string = params.urlencode()
        return f"?{query_string}" if query_string else "?"

    def _build_workflow_filters(self, params):
        self._ensure_candidate_map()
        current = params.get("workflow_status", "all")
        options = [
            ("all", "All"),
            ("ready", "Ready"),
            ("already_issued", "Already Issued"),
            ("due_in_15", "Due in 15 Days"),
            ("due_in_30", "Due in 30 Days"),
            ("setup_needed", "Setup Needed"),
        ]
        counts = {"all": len(self._candidate_map)}
        for value, _label in options[1:]:
            counts[value] = sum(1 for candidate in self._candidate_map.values() if candidate["status"] == value)
        return [
            {
                "label": f"{label} ({counts.get(value, 0)})",
                "url": self._replace_query(params, workflow_status=value),
                "active": current == value,
            }
            for value, label in options
        ]

    def _build_active_filters(self, params):
        current = params.get("active_state", "all")
        options = [("all", "All"), ("active", "Active"), ("inactive", "Inactive")]
        base_queryset = self.model.objects.all()
        counts = {
            "all": base_queryset.count(),
            "active": base_queryset.filter(is_active=True).count(),
            "inactive": base_queryset.filter(is_active=False).count(),
        }
        return [
            {
                "label": f"{label} ({counts.get(value, 0)})",
                "url": self._replace_query(params, active_state=value),
                "active": current == value,
            }
            for value, label in options
        ]

    def _build_term_filters(self, params):
        current = params.get("term", "all")
        options = [("all", "All Terms"), ("3", "3 Months"), ("6", "6 Months"), ("9", "9 Months"), ("12", "12 Months")]
        base_queryset = self.model.objects.all()
        counts = {
            "all": base_queryset.count(),
            "3": base_queryset.filter(billing_term=3).count(),
            "6": base_queryset.filter(billing_term=6).count(),
            "9": base_queryset.filter(billing_term=9).count(),
            "12": base_queryset.filter(billing_term=12).count(),
        }
        return [
            {
                "label": f"{label} ({counts.get(value, 0)})",
                "url": self._replace_query(params, term=value),
                "active": current == value,
            }
            for value, label in options
        ]

    def _latest_invoice(self, obj):
        prefetched = getattr(obj, "prefetched_invoices", None)
        if prefetched is not None:
            return prefetched[0] if prefetched else None
        return obj.invoices.exclude(status=Invoice.STATUS_VOID).order_by("-period_start", "-id").first()

    def _build_customer_workflow(self, obj):
        latest_invoice = self._latest_invoice(obj)
        today = timezone.localdate()
        if latest_invoice:
            period_start = latest_invoice.next_period_start
            period_end = latest_invoice.next_period_end
            issue_date = period_start - timedelta(days=15)
            existing_invoice = Invoice.objects.filter(customer=obj, period_start=period_start, period_end=period_end).first()
            if existing_invoice:
                status = "already_issued"
                message = f"Already issued: {existing_invoice.invoice_number}"
            else:
                days_until_issue = (issue_date - today).days
                if days_until_issue <= 0:
                    status = "ready"
                    message = "Ready to generate."
                elif days_until_issue <= 15:
                    status = "due_in_15"
                    message = f"Available on {issue_date:%Y-%m-%d}"
                elif days_until_issue <= 30:
                    status = "due_in_30"
                    message = f"Available on {issue_date:%Y-%m-%d}"
                else:
                    status = "already_issued"
                    message = f"Current invoice already issued. Next opens on {issue_date:%Y-%m-%d}"
            return {
                "status": status,
                "message": message,
                "period_start": period_start,
                "period_end": period_end,
                "issue_date": issue_date,
            }
        if obj.is_active:
            return {"status": "setup_needed", "message": "Customer needs first billing date and active service."}
        return {"status": "inactive", "message": "Customer is inactive."}

    def _candidate_for(self, obj):
        candidate = getattr(self, "_candidate_map", {}).get(obj.pk)
        if candidate:
            return candidate
        return self._build_customer_workflow(obj)

    @admin.display(ordering="invoice_total", description="Invoices")
    def invoice_count(self, obj):
        return obj.invoice_total

    @admin.display(description="Open Balance")
    def open_balance(self, obj):
        total = self._customer_open_balance(obj, as_of_date=timezone.localdate())
        return f"${total:.2f}"

    @admin.display(ordering="last_payment_date", description="Last Payment")
    def last_payment_date_display(self, obj):
        if getattr(obj, "last_payment_date", None):
            return obj.last_payment_date.strftime("%m/%d/%Y")
        return "-"

    @admin.display(description="Latest Invoice")
    def latest_invoice_number(self, obj):
        invoice = self._latest_invoice(obj)
        return invoice.invoice_number if invoice else "-"

    @admin.display(description="Next Status")
    def next_invoice_status(self, obj):
        candidate = self._candidate_for(obj)
        status = candidate["status"]
        labels = {
            "ready": "Ready",
            "already_issued": "Already Issued",
            "due_in_15": "Due in 15 Days",
            "due_in_30": "Due in 30 Days",
            "setup_needed": "Setup Needed",
            "inactive": "Inactive",
        }
        if status == "ready":
            return format_html('<span style="color:#b91c1c;font-weight:700;">! {}</span>', labels["ready"])
        return labels.get(status, status)

    @admin.display(description="Next Billing Period")
    def next_invoice_period(self, obj):
        candidate = self._candidate_for(obj)
        if candidate.get("period_start") and candidate.get("period_end"):
            return f'{candidate["period_start"]:%m/%d/%Y} - {candidate["period_end"]:%m/%d/%Y}'
        return "-"

    @admin.display(description="Next Issue Date")
    def next_issue_date(self, obj):
        candidate = self._candidate_for(obj)
        if candidate.get("issue_date"):
            return candidate["issue_date"].strftime("%m/%d/%Y")
        return "-"

    @admin.display(description="Invoice List")
    def view_invoices_link(self, obj):
        url = reverse("admin:billing_invoice_changelist")
        return format_html('<a href="{}?customer__id__exact={}">View invoices</a>', url, obj.pk)

    @admin.display(description="Payments")
    def payment_count(self, obj):
        return getattr(obj, "payment_total", obj.payments.count())

    @admin.display(description="Payment Actions")
    def payment_actions(self, obj):
        add_url = reverse("admin:payments_payment_quick_entry")
        list_url = reverse("admin:payments_payment_changelist")
        return format_html(
            '<a href="{}?customer={}">Add payment</a> | <a href="{}?customer__id__exact={}">View payments</a>',
            add_url,
            obj.pk,
            list_url,
            obj.pk,
        )


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    form = ServiceForm
    list_display = ("service_name", "customer", "billing_amount", "activation_date", "is_active")
    search_fields = ("service_name", "customer__account_number", "customer__name")
