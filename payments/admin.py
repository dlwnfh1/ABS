import csv
import mimetypes
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.widgets import AutocompleteSelect
from django.contrib.admin.views.main import ChangeList
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.text import slugify
from xhtml2pdf import pisa

from customers.models import Customer
from billing.pdf_utils import ensure_md5_compat, logo_symbol_data_uri
from reports.models import SystemSetting

from .models import Payment, PaymentAllocation


class PaymentChangeList(ChangeList):
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


class PaymentAllocationInline(admin.TabularInline):
    model = PaymentAllocation
    extra = 0
    can_delete = False
    readonly_fields = ("invoice", "amount", "created_at")


class QuickPaymentForm(forms.Form):
    payment_date = forms.DateField(initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"}))
    amount = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0.01)
    method = forms.ChoiceField(choices=Payment.METHOD_CHOICES, initial=Payment.METHOD_CHECK)
    reference_number = forms.CharField(max_length=100, required=False)
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, customer_field=None, admin_site=None, **kwargs):
        super().__init__(*args, **kwargs)
        widget = None
        if customer_field is not None and admin_site is not None:
            widget = AutocompleteSelect(customer_field, admin_site)
        self.fields["customer"] = forms.ModelChoiceField(
            queryset=Customer.objects.filter(is_active=True).order_by("name", "account_number"),
            widget=widget,
        )
        self.order_fields([
            "customer",
            "payment_date",
            "amount",
            "method",
            "reference_number",
            "note",
        ])


class VoidPaymentForm(forms.Form):
    void_reason = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))


class CheckScanUploadForm(forms.Form):
    scanned_check = forms.FileField(
        help_text="Upload a payment attachment file in PDF, JPG, JPEG, or PNG format."
    )


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    custom_filter_params = {"status_filter", "method_filter"}
    change_form_template = "admin/payments/payment/change_form.html"
    change_list_template = "admin/payments/payment/change_list.html"
    list_display = (
        "customer",
        "payment_date",
        "amount",
        "payment_status",
        "scanned_check_link",
        "applied_amount",
        "unapplied_amount",
        "method",
        "reference_number",
    )
    search_fields = ("customer__name", "customer__account_number", "reference_number")
    autocomplete_fields = ("customer",)
    inlines = [PaymentAllocationInline]
    list_select_related = ("customer",)
    actions = ["void_selected_payments"]
    readonly_fields = (
        "customer",
        "payment_date",
        "amount",
        "method",
        "reference_number",
        "note",
        "payment_status",
        "scanned_check_link",
        "applied_amount",
        "unapplied_amount",
        "voided_at",
        "void_reason",
    )

    def get_changelist(self, request, **kwargs):
        return PaymentChangeList

    @staticmethod
    def request_query_dict(query_string):
        from django.http import QueryDict

        if query_string.startswith("?"):
            query_string = query_string[1:]
        return QueryDict(query_string, mutable=True)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path("quick-entry/", self.admin_site.admin_view(self.quick_entry_view), name="payments_payment_quick_entry"),
            path("import-csv/", self.admin_site.admin_view(self.import_csv_view), name="payments_payment_import_csv"),
            path("export-csv/", self.admin_site.admin_view(self.export_csv_view), name="payments_payment_export_csv"),
            path("attach-scan/", self.admin_site.admin_view(self.attach_scan_view), name="payments_payment_attach_scan"),
            path("<path:object_id>/void/", self.admin_site.admin_view(self.void_view), name="payments_payment_void"),
            path("<path:object_id>/scan/", self.admin_site.admin_view(self.scan_file_view), name="payments_payment_scan_file"),
            path("<path:object_id>/receipt/pdf/", self.admin_site.admin_view(self.receipt_pdf_view), name="payments_payment_receipt_pdf"),
            path("<path:object_id>/receipt/print/", self.admin_site.admin_view(self.receipt_print_view), name="payments_payment_receipt_print"),
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

        status_filter = original_get.get("status_filter", "all")
        method_filter = original_get.get("method_filter", "all")
        extra_context = extra_context or {}
        extra_context["quick_payment_url"] = reverse("admin:payments_payment_quick_entry")
        extra_context["import_csv_url"] = reverse("admin:payments_payment_import_csv")
        extra_context["export_csv_url"] = reverse("admin:payments_payment_export_csv")
        extra_context["attach_scan_url"] = reverse("admin:payments_payment_attach_scan")
        extra_context["status_filters"] = self._build_status_filters(request, status_filter, method_filter)
        extra_context["method_filters"] = self._build_method_filters(request, status_filter, method_filter)
        preserved_filters = original_get.copy()
        for key in list(preserved_filters.keys()):
            if key not in self.custom_filter_params or not preserved_filters.get(key):
                preserved_filters.pop(key, None)
        extra_context["custom_preserved_filters"] = preserved_filters.urlencode()
        return super().changelist_view(request, extra_context=extra_context)

    def get_queryset(self, request):
        filter_params = getattr(request, "_custom_filter_params", request.GET)
        queryset = super().get_queryset(request)
        status_filter = filter_params.get("status_filter", "all")
        method_filter = filter_params.get("method_filter", "all")
        if status_filter == "applied":
            queryset = queryset.filter(is_voided=False)
        elif status_filter == "voided":
            queryset = queryset.filter(is_voided=True)
        if method_filter != "all":
            queryset = queryset.filter(method=method_filter)
        return queryset

    def has_delete_permission(self, request, obj=None):
        return False

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        payment = self.get_object(request, object_id)
        if payment and not payment.is_voided:
            extra_context["void_payment_url"] = reverse("admin:payments_payment_void", args=[object_id])
        if payment:
            extra_context["receipt_pdf_url"] = reverse("admin:payments_payment_receipt_pdf", args=[object_id])
            extra_context["receipt_print_url"] = reverse("admin:payments_payment_receipt_print", args=[object_id])
            if payment.scanned_check_path:
                extra_context["scan_file_url"] = reverse("admin:payments_payment_scan_file", args=[object_id])
            extra_context["attach_scan_url"] = reverse("admin:payments_payment_attach_scan") + f"?ids={object_id}"
        extra_context["show_save_buttons"] = False
        return super().change_view(request, object_id, form_url=form_url, extra_context=extra_context)

    def add_view(self, request, form_url="", extra_context=None):
        customer_id = request.GET.get("customer")
        quick_url = reverse("admin:payments_payment_quick_entry")
        if customer_id:
            quick_url = f"{quick_url}?customer={customer_id}"
        return redirect(quick_url)

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        customer_id = request.GET.get("customer")
        if customer_id:
            initial["customer"] = customer_id
        return initial

    def quick_entry_view(self, request):
        customer_field = Payment._meta.get_field("customer")
        selected_customer = None
        preview = None
        action = "preview"

        initial = {}
        customer_id = request.GET.get("customer")
        if customer_id and customer_id.isdigit():
            initial["customer"] = customer_id
        if request.GET.get("payment_date"):
            initial["payment_date"] = request.GET.get("payment_date")
        if request.GET.get("method") in {choice[0] for choice in Payment.METHOD_CHOICES}:
            initial["method"] = request.GET.get("method")

        if request.method == "POST":
            form = QuickPaymentForm(request.POST, customer_field=customer_field, admin_site=self.admin_site)
            action = request.POST.get("action", "preview")
            if form.is_valid():
                selected_customer = form.cleaned_data["customer"]
                preview = Payment.allocation_preview(
                    selected_customer,
                    form.cleaned_data["payment_date"],
                    form.cleaned_data["amount"],
                )
                if action in {"save", "save_new"}:
                    payment = Payment.objects.create(
                        customer=selected_customer,
                        payment_date=form.cleaned_data["payment_date"],
                        amount=form.cleaned_data["amount"],
                        method=form.cleaned_data["method"],
                        reference_number=form.cleaned_data["reference_number"],
                        note=form.cleaned_data["note"],
                    )
                    self.message_user(
                        request,
                        f"Payment of ${payment.amount:.2f} saved for {selected_customer.name} and applied automatically.",
                        level=messages.SUCCESS,
                    )
                    if action == "save":
                        return redirect("admin:payments_payment_change", object_id=payment.pk)
                    base_url = reverse("admin:payments_payment_quick_entry")
                    query = (
                        f"saved_payment={payment.pk}"
                        f"&payment_date={form.cleaned_data['payment_date']:%Y-%m-%d}"
                        f"&method={form.cleaned_data['method']}"
                        f"&focus=amount"
                    )
                    return redirect(f"{base_url}?{query}")
        else:
            form = QuickPaymentForm(initial=initial, customer_field=customer_field, admin_site=self.admin_site)
            selected_customer = form.initial.get("customer")
            if selected_customer:
                selected_customer = Customer.objects.filter(pk=selected_customer).first()

        customer_summary = self._build_customer_summary(selected_customer)
        saved_payment = None
        saved_payment_id = request.GET.get("saved_payment")
        if saved_payment_id and saved_payment_id.isdigit():
            saved_payment = Payment.objects.filter(pk=saved_payment_id).select_related("customer").first()
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Quick Payment",
            "form": form,
            "media": self.media + form.media,
            "customer_summary": customer_summary,
            "selected_customer": selected_customer,
            "preview": preview,
            "action": action,
            "saved_payment": saved_payment,
            "saved_receipt_pdf_url": reverse("admin:payments_payment_receipt_pdf", args=[saved_payment.pk]) if saved_payment else "",
            "saved_receipt_print_url": reverse("admin:payments_payment_receipt_print", args=[saved_payment.pk]) if saved_payment else "",
            "saved_attach_scan_url": reverse("admin:payments_payment_attach_scan") + f"?ids={saved_payment.pk}" if saved_payment else "",
            "payment_changelist_url": reverse("admin:payments_payment_changelist"),
        }
        return TemplateResponse(request, "admin/payments/payment/quick_entry.html", context)

    def _build_status_filters(self, request, status_filter, method_filter):
        items = [
            ("all", "All"),
            ("applied", "Applied"),
            ("voided", "Voided"),
        ]
        return [
            {
                "label": label,
                "active": value == status_filter,
                "url": self._build_filter_url(request, status_filter=value, method_filter=method_filter),
            }
            for value, label in items
        ]

    def _build_method_filters(self, request, status_filter, method_filter):
        items = [("all", "All Methods")] + list(Payment.METHOD_CHOICES)
        return [
            {
                "label": label if value == "all" else label,
                "active": value == method_filter,
                "url": self._build_filter_url(request, status_filter=status_filter, method_filter=value),
            }
            for value, label in items
        ]

    def _build_filter_url(self, request, **updates):
        query = request.GET.copy()
        for key, value in updates.items():
            if value == "all":
                query.pop(key, None)
            else:
                query[key] = value
        encoded = query.urlencode()
        return f"?{encoded}" if encoded else "?"

    def receipt_pdf_view(self, request, object_id):
        return self._render_receipt_pdf_response(request, object_id, as_attachment=True)

    def receipt_print_view(self, request, object_id):
        return self._render_receipt_pdf_response(request, object_id, as_attachment=False)

    def import_csv_view(self, request):
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Import Payments CSV",
            "expected_columns": [
                "account_number",
                "customer_name",
                "payment_date",
                "amount",
                "method",
                "reference_number",
                "note",
            ],
        }
        if request.method == "POST":
            upload = request.FILES.get("csv_file")
            if not upload:
                self.message_user(request, "Select a CSV file to import.", level=messages.ERROR)
                return TemplateResponse(request, "admin/payments/payment/import_csv.html", context)
            try:
                created_count, skipped_count = self._import_csv_file(upload)
            except ValueError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
                return TemplateResponse(request, "admin/payments/payment/import_csv.html", context)
            self.message_user(
                request,
                f"Imported payments successfully. Created: {created_count}. Skipped duplicates: {skipped_count}.",
                level=messages.SUCCESS,
            )
            return redirect("admin:payments_payment_changelist")
        return TemplateResponse(request, "admin/payments/payment/import_csv.html", context)

    def attach_scan_view(self, request):
        selected_ids = request.POST.getlist("_selected_action") or request.GET.getlist("ids")
        if request.method == "GET" and not selected_ids:
            raw_ids = request.GET.get("ids", "")
            selected_ids = [pk for pk in raw_ids.split(",") if pk.isdigit()]

        payments = list(
            Payment.objects.filter(pk__in=selected_ids).select_related("customer").order_by("-payment_date", "-id")
        )
        changelist_url = reverse("admin:payments_payment_changelist")

        if len(payments) != 1:
            self.message_user(request, "Select exactly one payment to attach a payment file.", level=messages.WARNING)
            return redirect(changelist_url)

        payment = payments[0]
        if payment.is_voided:
            self.message_user(request, "Voided payments cannot receive a payment attachment.", level=messages.WARNING)
            return redirect(changelist_url)

        form = CheckScanUploadForm(request.POST or None, request.FILES or None)
        if request.method == "POST" and form.is_valid():
            upload = form.cleaned_data["scanned_check"]
            try:
                saved_path = self._save_scanned_check_file(payment, upload)
            except ValueError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
            else:
                uploaded_at = timezone.now()
                Payment.objects.filter(pk=payment.pk).update(
                    scanned_check_path=str(saved_path),
                    scanned_check_uploaded_at=uploaded_at,
                )
                payment.scanned_check_path = str(saved_path)
                payment.scanned_check_uploaded_at = uploaded_at
                self.message_user(
                    request,
                    f"Payment attachment saved for {payment.customer.name} at {saved_path}.",
                    level=messages.SUCCESS,
                )
                return redirect("admin:payments_payment_changelist")

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Attach Scanned Check",
            "page_title": "Attach Payment File",
            "payment": payment,
            "form": form,
            "changelist_url": changelist_url,
        }
        return TemplateResponse(request, "admin/payments/payment/attach_scan.html", context)

    def scan_file_view(self, request, object_id):
        payment = self.get_object(request, object_id)
        if not payment or not payment.scanned_check_path:
            raise Http404("Scanned check file not found.")
        path = Path(payment.scanned_check_path)
        if not path.exists() or not path.is_file():
            raise Http404("Scanned check file is missing from disk.")
        content_type, _ = mimetypes.guess_type(str(path))
        response = FileResponse(path.open("rb"), content_type=content_type or "application/octet-stream")
        response["Content-Disposition"] = f'inline; filename="{path.name}"'
        return response

    def export_csv_view(self, request):
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="payments_export.csv"'
        response.write("\ufeff")
        writer = csv.writer(response)
        writer.writerow(
            [
                "account_number",
                "customer_name",
                "payment_date",
                "amount",
                "method",
                "reference_number",
                "note",
            ]
        )
        payments = (
            Payment.objects.filter(is_voided=False)
            .select_related("customer")
            .order_by("-payment_date", "-id")
        )
        for payment in payments:
            writer.writerow(
                [
                    payment.customer.account_number,
                    payment.customer.name,
                    payment.payment_date.strftime("%m-%d-%Y") if payment.payment_date else "",
                    f"{payment.amount:.2f}",
                    payment.get_method_display(),
                    payment.reference_number,
                    payment.note,
                ]
            )
        return response

    def _save_scanned_check_file(self, payment, upload):
        settings_obj = SystemSetting.get_solo()
        if not settings_obj or not settings_obj.payment_check_scan_folder:
            raise ValueError("Set the Check Scan Folder first in Reports -> System Setting.")

        suffix = Path(upload.name).suffix.lower()
        if suffix not in {".pdf", ".jpg", ".jpeg", ".png"}:
            raise ValueError("Scanned check file must be a PDF, JPG, JPEG, or PNG file.")

        base_folder = Path(settings_obj.payment_check_scan_folder)
        date_folder = base_folder / timezone.localdate().strftime("%Y-%m-%d")
        date_folder.mkdir(parents=True, exist_ok=True)
        customer_slug = slugify(payment.customer.name) or f"customer-{payment.customer_id}"
        reference_slug = slugify(payment.reference_number) if payment.reference_number else "no-reference"
        filename = (
            f"{timezone.localdate():%Y-%m-%d}_{payment.customer.account_number}_{customer_slug}_"
            f"PAY-{payment.pk:05d}_{reference_slug}{suffix}"
        )
        output_path = date_folder / filename
        with output_path.open("wb") as destination:
            for chunk in upload.chunks():
                destination.write(chunk)
        return output_path

    def void_view(self, request, object_id):
        payment = self.get_object(request, object_id)
        if not payment:
            self.message_user(request, "Payment was not found.", level=messages.ERROR)
            return redirect("admin:payments_payment_changelist")
        if payment.is_voided:
            self.message_user(request, "This payment is already voided.", level=messages.WARNING)
            return redirect("admin:payments_payment_change", object_id=object_id)

        form = VoidPaymentForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            payment.void(reason=form.cleaned_data["void_reason"])
            self.message_user(
                request,
                f"Payment of ${payment.amount:.2f} for {payment.customer.name} was voided.",
                level=messages.SUCCESS,
            )
            return redirect("admin:payments_payment_change", object_id=object_id)

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Void Payment",
            "payment": payment,
            "form": form,
            "original": payment,
        }
        return TemplateResponse(request, "admin/payments/payment/void_payment.html", context)

    def _build_customer_summary(self, customer):
        if not customer:
            return None
        latest_payment = customer.payments.filter(is_voided=False).order_by("-payment_date", "-id").first()
        return {
            "name": customer.name,
            "account_number": customer.account_number,
            "open_balance": customer.open_balance_as_of(),
            "last_payment": latest_payment,
            "current_billing_amount": customer.current_billing_amount,
            "billing_term": customer.get_billing_term_display(),
        }

    def get_fields(self, request, obj=None):
        return [
            "customer",
            "payment_date",
            "amount",
            "method",
            "reference_number",
            "note",
            "payment_status",
            "scanned_check_link",
            "applied_amount",
            "unapplied_amount",
            "voided_at",
            "void_reason",
        ]

    def _import_csv_file(self, upload):
        raw_bytes = upload.read()
        try:
            decoded_text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                decoded_text = raw_bytes.decode("cp949")
            except UnicodeDecodeError as exc:
                raise ValueError("CSV encoding must be UTF-8, UTF-8 with BOM, or CP949.") from exc

        decoded_lines = decoded_text.splitlines()
        reader = csv.DictReader(decoded_lines)
        required_columns = {"account_number", "payment_date", "amount", "method"}
        if not reader.fieldnames:
            raise ValueError("The CSV file is empty.")
        missing = required_columns.difference(reader.fieldnames)
        if missing:
            raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")

        created_count = 0
        skipped_count = 0

        for index, row in enumerate(reader, start=2):
            account_number = (row.get("account_number") or "").strip()
            if not account_number:
                raise ValueError(f"Row {index}: account_number is required.")

            customer = Customer.objects.filter(account_number=account_number).first()
            if not customer:
                raise ValueError(f"Row {index}: customer with account_number {account_number} was not found.")

            payment_date = self._parse_optional_date(row.get("payment_date"), "payment_date", index)
            amount = self._parse_decimal(row.get("amount"), "amount", index)
            method = self._parse_method(row.get("method"), index)
            reference_number = (row.get("reference_number") or "").strip()
            note = (row.get("note") or "").strip()

            duplicate_qs = Payment.objects.filter(
                customer=customer,
                payment_date=payment_date,
                amount=amount,
                method=method,
                reference_number=reference_number,
                is_voided=False,
            )
            if duplicate_qs.exists():
                skipped_count += 1
                continue

            Payment.objects.create(
                customer=customer,
                payment_date=payment_date,
                amount=amount,
                method=method,
                reference_number=reference_number,
                note=note,
            )
            created_count += 1

        return created_count, skipped_count

    def _parse_decimal(self, value, field_name, row_number):
        try:
            return Decimal(str(value).strip())
        except (InvalidOperation, AttributeError):
            raise ValueError(f"Row {row_number}: {field_name} must be a valid decimal number.")

    def _parse_optional_date(self, value, field_name, row_number):
        raw = (value or "").strip()
        if not raw:
            raise ValueError(f"Row {row_number}: {field_name} is required.")
        parsed = self._parse_csv_date(raw)
        if not parsed:
            raise ValueError(f"Row {row_number}: {field_name} must be in MM-DD-YYYY or M/D/YYYY format.")
        return parsed

    def _parse_csv_date(self, value):
        for fmt in ("%m-%d-%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        return None

    def _parse_method(self, value, row_number):
        raw = (value or "").strip().lower()
        method_map = {
            "cash": Payment.METHOD_CASH,
            "check": Payment.METHOD_CHECK,
            "credit_card": Payment.METHOD_CREDIT_CARD,
            "credit card": Payment.METHOD_CREDIT_CARD,
            "card": Payment.METHOD_CREDIT_CARD,
            "ach": Payment.METHOD_ACH,
            "other": Payment.METHOD_OTHER,
        }
        parsed = method_map.get(raw)
        if not parsed:
            raise ValueError(f"Row {row_number}: method must be one of cash, check, credit card, ach, or other.")
        return parsed

    def _render_receipt_pdf_response(self, request, object_id, as_attachment):
        payment = self.get_object(request, object_id)
        if not payment:
            self.message_user(request, "Payment was not found.", level=messages.ERROR)
            return redirect("admin:payments_payment_changelist")

        pdf_bytes = self._render_receipt_pdf_bytes(payment)
        if not pdf_bytes:
            self.message_user(request, "Receipt PDF generation failed.", level=messages.ERROR)
            return redirect("admin:payments_payment_change", object_id=object_id)

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        disposition = "attachment" if as_attachment else "inline"
        response["Content-Disposition"] = f'{disposition}; filename="PAY-{payment.pk:05d}.pdf"'
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

    def _render_receipt_pdf_bytes(self, payment):
        ensure_md5_compat()
        allocations = list(payment.allocations.select_related("invoice").order_by("invoice__period_start", "id"))
        context = {
            "payment": payment,
            "customer": payment.customer,
            "allocations": allocations,
            "logo_symbol_data_uri": logo_symbol_data_uri(),
            "generated_on": timezone.localtime(),
        }
        html = render_to_string("admin/payments/payment/receipt_pdf.html", context)
        output = HttpResponse(content_type="application/pdf")
        result = pisa.CreatePDF(html, dest=output, encoding="utf-8")
        if result.err:
            return b""
        return output.content

    @admin.display(description="Status")
    def payment_status(self, obj):
        if obj.is_voided:
            return format_html(
                '<span style="display:inline-flex;align-items:center;padding:3px 10px;border-radius:999px;'
                'background:#fee2e2;color:#b91c1c;font-weight:700;">Voided</span>'
            )
        return format_html(
            '<span style="display:inline-flex;align-items:center;padding:3px 10px;border-radius:999px;'
            'background:#dcfce7;color:#166534;font-weight:700;">Applied</span>'
        )

    @admin.display(description="Scanned Check")
    def scanned_check_link(self, obj):
        if not obj.scanned_check_path:
            return "-"
        url = reverse("admin:payments_payment_scan_file", args=[obj.pk])
        return format_html('<a href="{}" target="_blank">Open attachment</a>', url)

    @admin.action(description="Void selected payments")
    def void_selected_payments(self, request, queryset):
        voided = 0
        for payment in queryset.filter(is_voided=False):
            payment.void(reason="Voided from payment list.")
            voided += 1
        if voided:
            self.message_user(request, f"Voided {voided} payment(s).", level=messages.SUCCESS)
        else:
            self.message_user(request, "No active payments were selected.", level=messages.WARNING)


@admin.register(PaymentAllocation)
class PaymentAllocationAdmin(admin.ModelAdmin):
    list_display = ("payment", "invoice", "amount", "created_at")
    search_fields = ("payment__customer__name", "payment__customer__account_number", "invoice__invoice_number")
    autocomplete_fields = ("payment", "invoice")

    def get_model_perms(self, request):
        return {}
