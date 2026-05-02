from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Max, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from xhtml2pdf import pisa

from billing.models import Invoice, add_months
from billing.pdf_utils import (
    ensure_md5_compat,
    get_invoice_output_base_folder,
    list_saved_invoice_pdf_records,
    merge_saved_invoice_pdf_records,
    logo_symbol_data_uri,
    portal_logo_data_uri,
    render_invoice_pdf_bytes,
)
from customers.models import Customer, Service
from payments.models import Payment, PaymentAllocation
from reports.models import InvoiceGenerationBatch, SavedInvoicePDF, SystemSetting

from .forms import PortalCustomerCreateForm, PortalCustomerEditForm, PortalQuickPaymentForm


def _format_auto_ach_review_summary(customers):
    if not customers:
        return ""
    visible = [customer.name for customer in customers[:5]]
    if len(customers) > 5:
        visible.append(f"외 {len(customers) - 5}명")
    return ", ".join(visible)


def _portal_customer_queryset():
    invoice_qs = (
        Invoice.objects.exclude(status=Invoice.STATUS_VOID)
        .prefetch_related(
            Prefetch(
                "allocations",
                queryset=PaymentAllocation.objects.select_related("payment")
                .filter(payment__is_voided=False)
                .order_by("payment__payment_date", "id"),
                to_attr="_prefetched_valid_allocations",
            )
        )
        .order_by("-period_start", "-id")
    )
    return Customer.objects.prefetch_related(
        Prefetch(
            "services",
            queryset=Service.objects.filter(is_active=True).order_by("id"),
            to_attr="_prefetched_active_services",
        ),
        Prefetch(
            "invoices",
            queryset=invoice_qs,
            to_attr="_prefetched_nonvoid_invoices",
        ),
        Prefetch(
            "payments",
            queryset=Payment.objects.filter(is_voided=False).order_by("-payment_date", "-created_at", "-id"),
            to_attr="_prefetched_nonvoid_payments",
        ),
    )


def _portal_context(request, **extra):
    today = timezone.localdate()
    unprinted_batch_count = InvoiceGenerationBatch.objects.filter(is_printed=False).count()
    unprinted_invoice_count = SavedInvoicePDF.objects.filter(batch__is_printed=False, marker="CURRENT").count()
    auto_ach_review_customers = [
        customer
        for customer in _portal_customer_queryset().filter(is_active=True, auto_ach=True).order_by("name", "account_number")
        if customer.auto_ach_review_needed(today)
    ]
    auto_ach_review_count = len(auto_ach_review_customers)
    auto_ach_review_summary = _format_auto_ach_review_summary(auto_ach_review_customers)
    return {
        "nav_items": [
            {"label": "Customers", "url": reverse("portal:customer_list"), "key": "customers"},
            {"label": "Quick Payment", "url": reverse("portal:quick_payment"), "key": "payments"},
            {"label": "Invoice Dispatch", "url": reverse("portal:saved_invoice_list"), "key": "dispatch"},
            {"label": "Reports", "url": reverse("portal:report_index"), "key": "reports"},
        ],
        "unprinted_batch_count": unprinted_batch_count,
        "unprinted_invoice_count": unprinted_invoice_count,
        "auto_ach_review_count": auto_ach_review_count,
        "auto_ach_review_summary": auto_ach_review_summary,
        "logo_symbol_data_uri": portal_logo_data_uri(),
        **extra,
    }


def _parse_iso_date(value, fallback):
    if not value:
        return fallback
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return fallback


def _paginate_items(request, items, per_page):
    paginator = Paginator(items, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)
    query = request.GET.copy()
    query.pop("page", None)
    return page_obj, query.urlencode(), paginator.get_elided_page_range(number=page_obj.number, on_each_side=2, on_ends=1)


def _save_payment_attachment(payment, upload):
    settings_obj = SystemSetting.get_solo()
    if not settings_obj or not settings_obj.payment_check_scan_folder:
        raise ValidationError("Set the payment attachment folder first in Reports -> System Setting.")

    suffix = Path(upload.name).suffix.lower()
    if suffix not in {".pdf", ".jpg", ".jpeg", ".png"}:
        raise ValidationError("Attachment file must be a PDF, JPG, JPEG, or PNG file.")

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

    payment.scanned_check_path = str(output_path)
    payment.scanned_check_uploaded_at = timezone.now()
    payment.save(update_fields=["scanned_check_path", "scanned_check_uploaded_at"])
    return output_path


def _render_receipt_pdf_bytes(payment):
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
    ensure_md5_compat()
    result = pisa.CreatePDF(html, dest=output, encoding="utf-8")
    if result.err:
        return b""
    return output.content


def _pdf_response(pdf_bytes, filename, as_attachment):
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    disposition = "attachment" if as_attachment else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


def _customer_summary(customer):
    if not customer:
        return None
    latest_payment = next(iter(customer._active_payments_cache()), None)
    return {
        "name": customer.name,
        "account_number": customer.account_number,
        "open_balance": customer.open_balance_as_of(),
        "last_payment": latest_payment,
        "current_billing_amount": customer.current_billing_amount,
        "billing_term": customer.get_billing_term_display(),
    }


def _add_months_local(source_date: date, months: int) -> date:
    month_index = source_date.month - 1 + months
    year = source_date.year + month_index // 12
    month = month_index % 12 + 1
    day = min(source_date.day, monthrange(year, month)[1])
    return date(year, month, day)


def _terms_overdue(customer, due_date, today):
    if not due_date or due_date >= today:
        return 0
    term_months = int(customer.billing_term)
    terms = 0
    cursor = due_date
    while cursor < today:
        terms += 1
        cursor = _add_months_local(cursor, term_months)
    return terms


def _build_ar_aging_data():
    today = timezone.localdate()
    rows = []
    totals = {
        "current": Decimal("0.00"),
        "term_1": Decimal("0.00"),
        "term_2": Decimal("0.00"),
        "term_3_plus": Decimal("0.00"),
        "total": Decimal("0.00"),
    }
    for customer in _portal_customer_queryset().filter(is_active=True).order_by("name", "account_number"):
        invoices = sorted(customer._nonvoid_invoices_cache(), key=lambda invoice: (invoice.period_start, invoice.id))
        bucket_totals = {key: Decimal("0.00") for key in ("current", "term_1", "term_2", "term_3_plus")}
        for invoice in invoices:
            if invoice.issue_date and invoice.issue_date > today:
                continue
            amount = invoice.outstanding_amount_as_of(today)
            if amount <= Decimal("0.00"):
                continue
            overdue_terms = _terms_overdue(customer, invoice.due_date, today)
            if overdue_terms <= 0:
                bucket = "current"
            elif overdue_terms == 1:
                bucket = "term_1"
            elif overdue_terms == 2:
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
            }
        )
    return today, rows, totals


def _build_payments_report_data(date_from, date_to):
    payments = list(
        Payment.objects.select_related("customer")
        .filter(is_voided=False, payment_date__gte=date_from, payment_date__lte=date_to)
        .order_by("-payment_date", "-id")
    )
    method_totals = {}
    total_amount = Decimal("0.00")
    for payment in payments:
        label = payment.get_method_display()
        method_totals.setdefault(label, Decimal("0.00"))
        method_totals[label] += payment.amount
        total_amount += payment.amount
    return payments, method_totals, total_amount


def _build_overdue_customers_data():
    today = timezone.localdate()
    rows = []
    totals = {
        "customer_count": 0,
        "overdue_invoices": 0,
        "overdue_total": Decimal("0.00"),
        "open_total": Decimal("0.00"),
        "max_terms_overdue": 0,
    }
    for customer in _portal_customer_queryset().filter(is_active=True).order_by("name", "account_number"):
        overdue_entries = []
        invoices = sorted(customer._nonvoid_invoices_cache(), key=lambda invoice: (invoice.due_date or date.min, invoice.period_start, invoice.id))
        for invoice in invoices:
            if invoice.issue_date and invoice.issue_date > today:
                continue
            amount = invoice.outstanding_amount_as_of(today)
            if amount <= Decimal("0.00"):
                continue
            terms = _terms_overdue(customer, invoice.due_date, today)
            if terms <= 0:
                continue
            overdue_entries.append((invoice, amount, terms))
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
            }
        )
        totals["customer_count"] += 1
        totals["overdue_invoices"] += len(overdue_entries)
        totals["overdue_total"] += overdue_total
        totals["open_total"] += open_total
        totals["max_terms_overdue"] = max(totals["max_terms_overdue"], max_terms_overdue)
    return today, rows, totals


def _build_upcoming_billing_data():
    today = timezone.localdate()
    horizon = today + timedelta(days=30)
    rows = []
    totals = {"ready": 0, "due_in_15": 0, "due_in_30": 0, "total": 0, "projected_amount": Decimal("0.00")}
    term_counts = {3: 0, 6: 0, 9: 0, 12: 0}
    term_amounts = {3: Decimal("0.00"), 6: Decimal("0.00"), 9: Decimal("0.00"), 12: Decimal("0.00")}

    for customer in _portal_customer_queryset().filter(is_active=True).order_by("name", "account_number"):
        if not customer.first_billing_date or not customer._billable_services_cache():
            continue
        latest_invoice = next(iter(customer._nonvoid_invoices_cache()), None)
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
        rows.append(
            {
                "customer": customer,
                "status": status,
                "issue_date": issue_date,
                "period_start": period_start,
                "period_end": period_end,
                "billing_amount": billing_amount,
                "open_balance": customer.open_balance_as_of(today),
                "billing_term": customer.billing_term,
                "billing_term_label": customer.get_billing_term_display(),
            }
        )
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


def _customer_workflow_snapshot(customer, as_of_date):
    latest_invoice = next(iter(customer._nonvoid_invoices_cache()), None)
    if latest_invoice:
        period_start = latest_invoice.next_period_start
        period_end = latest_invoice.next_period_end
        issue_date = period_start - timedelta(days=15)
        days_until_issue = (issue_date - as_of_date).days
        if days_until_issue <= 0:
            status = "Ready"
        elif days_until_issue <= 15:
            status = "Due in 15 Days"
        elif days_until_issue <= 30:
            status = "Due in 30 Days"
        else:
            status = "Already Issued"
        return {
            "status": status,
            "period": f"{period_start:%m/%d/%Y} - {period_end:%m/%d/%Y}",
            "issue_date": issue_date,
        }
    if customer.is_active and customer.can_generate_initial_invoice():
        issue_date = customer.first_billing_date - timedelta(days=15)
        return {
            "status": "Ready" if issue_date <= as_of_date else "Due in 15 Days",
            "period": "",
            "issue_date": issue_date,
        }
    return {"status": "Setup Needed", "period": "", "issue_date": None}


@login_required(login_url="portal:login")
def dashboard_view(request):
    return redirect("portal:customer_list")


@login_required(login_url="portal:login")
def customer_list_view(request):
    query = (request.GET.get("q") or "").strip()
    status_filter = (request.GET.get("status") or "active").strip().lower()

    customers = _portal_customer_queryset().order_by("name", "account_number")
    if status_filter == "inactive":
        customers = customers.filter(is_active=False)
    elif status_filter == "all":
        pass
    else:
        status_filter = "active"
        customers = customers.filter(is_active=True)

    if query:
        customers = customers.filter(
            Q(name__icontains=query)
            | Q(account_number__icontains=query)
            | Q(billing_address1__icontains=query)
            | Q(billing_address2__icontains=query)
        ).order_by("name", "account_number")

    page_obj, page_query, page_numbers = _paginate_items(request, customers, 25)

    rows = []
    for customer in page_obj.object_list:
        workflow = _customer_workflow_snapshot(customer, timezone.localdate())
        latest_invoice = next(iter(customer._nonvoid_invoices_cache()), None)
        if latest_invoice:
            period = f"{latest_invoice.next_period_start:%m/%d/%Y} - {latest_invoice.next_period_end:%m/%d/%Y}"
        else:
            period = "-"
        last_payment = next(iter(customer._active_payments_cache()), None)
        rows.append(
            {
                "customer": customer,
                "workflow_status": workflow["status"],
                "next_billing_period": period,
                "open_balance": customer.open_balance_as_of(timezone.localdate()),
                "last_payment": last_payment,
                "quick_payment_url": f'{reverse("portal:quick_payment")}?customer={customer.pk}',
                "statement_url": f'{reverse("portal:customer_statement")}?customer={customer.pk}',
            }
        )

    return render(
        request,
        "portal/customers.html",
        _portal_context(
            request,
            active_nav="customers",
            title="Customers",
            rows=rows,
            query=query,
            status_filter=status_filter,
            page_obj=page_obj,
            page_query=page_query,
            page_numbers=page_numbers,
        ),
    )

@login_required(login_url="portal:login")
def customer_create_view(request):
    if request.method == "POST":
        form = PortalCustomerCreateForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                customer = Customer.objects.create(
                    name=form.cleaned_data["name"],
                    account_number=form.cleaned_data["account_number"],
                    billing_address1=form.cleaned_data["billing_address1"],
                    billing_address2=form.cleaned_data["billing_address2"],
                    email_address=form.cleaned_data["email_address"],
                    billing_term=int(form.cleaned_data["billing_term"]),
                    auto_ach=form.cleaned_data["auto_ach"],
                    tax_rate=form.cleaned_data["tax_rate"],
                    first_billing_date=form.cleaned_data["first_billing_date"],
                    is_active=form.cleaned_data["is_active"],
                )
                customer.services.create(
                    service_name=form.cleaned_data["service_name"] or "Alarm Monitoring Service",
                    service_address1=form.cleaned_data["service_address1"],
                    service_address2=form.cleaned_data["service_address2"],
                    billing_amount=form.cleaned_data["billing_amount"],
                    billing_status=form.cleaned_data["service_billing_status"],
                    is_active=form.cleaned_data["service_is_active"],
                )
            messages.success(request, f'{customer.name} ({customer.account_number}) was created successfully.')
            return redirect(reverse("portal:customer_list"))
    else:
        form = PortalCustomerCreateForm()

    return render(
        request,
        "portal/customer_create.html",
        _portal_context(
            request,
            active_nav="customers",
            title="New Customer",
            page_subtitle="Create a customer and first service in one step.",
            form=form,
        ),
    )


@login_required(login_url="portal:login")
def customer_edit_view(request, customer_id):
    customer = get_object_or_404(Customer, pk=customer_id)
    service = customer.services.order_by("id").first()

    if request.method == "POST":
        form = PortalCustomerEditForm(request.POST, customer=customer)
        if form.is_valid():
            with transaction.atomic():
                customer.name = form.cleaned_data["name"]
                customer.account_number = form.cleaned_data["account_number"]
                customer.billing_address1 = form.cleaned_data["billing_address1"]
                customer.billing_address2 = form.cleaned_data["billing_address2"]
                customer.email_address = form.cleaned_data["email_address"]
                customer.billing_term = int(form.cleaned_data["billing_term"])
                customer.auto_ach = form.cleaned_data["auto_ach"]
                customer.tax_rate = form.cleaned_data["tax_rate"]
                customer.first_billing_date = form.cleaned_data["first_billing_date"]
                customer.is_active = form.cleaned_data["is_active"]
                customer.save()

                if service is None:
                    service = customer.services.create(
                        service_name=form.cleaned_data["service_name"] or "Alarm Monitoring Service",
                        service_address1=form.cleaned_data["service_address1"],
                        service_address2=form.cleaned_data["service_address2"],
                        billing_amount=form.cleaned_data["billing_amount"],
                        billing_status=form.cleaned_data["service_billing_status"],
                        is_active=form.cleaned_data["service_is_active"],
                    )
                else:
                    service.service_name = form.cleaned_data["service_name"] or "Alarm Monitoring Service"
                    service.service_address1 = form.cleaned_data["service_address1"]
                    service.service_address2 = form.cleaned_data["service_address2"]
                    service.billing_amount = form.cleaned_data["billing_amount"]
                    service.billing_status = form.cleaned_data["service_billing_status"]
                    service.is_active = form.cleaned_data["service_is_active"]
                    service.save()

            messages.success(request, f'{customer.name} ({customer.account_number}) was updated successfully.')
            return redirect(f'{reverse("portal:customer_statement")}?customer={customer.pk}')
    else:
        initial = {
            "name": customer.name,
            "account_number": customer.account_number,
            "billing_address1": customer.billing_address1,
            "billing_address2": customer.billing_address2,
            "email_address": customer.email_address,
            "billing_term": customer.billing_term,
            "auto_ach": customer.auto_ach,
            "tax_rate": customer.tax_rate,
            "first_billing_date": customer.first_billing_date,
            "is_active": customer.is_active,
            "service_name": service.service_name if service else "Alarm Monitoring Service",
            "service_address1": service.service_address1 if service else customer.billing_address1,
            "service_address2": service.service_address2 if service else customer.billing_address2,
            "billing_amount": service.billing_amount if service else Decimal("0.00"),
            "service_billing_status": service.billing_status if service else Service.BILLING_STATUS_BILLABLE,
            "service_is_active": service.is_active if service else True,
        }
        form = PortalCustomerEditForm(initial=initial, customer=customer)

    return render(
        request,
        "portal/customer_create.html",
        _portal_context(
            request,
            active_nav="customers",
            title="Edit Customer",
            page_subtitle=f"Update customer information for {customer.name}.",
            form=form,
            submit_label="Save Changes",
            back_url=f'{reverse("portal:customer_statement")}?customer={customer.pk}',
            section_heading="Customer Information",
            service_heading="Service Information",
        ),
    )


@login_required(login_url="portal:login")
def quick_payment_view(request):
    selected_customer = None
    preview = None
    saved_payment = None
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
        form = PortalQuickPaymentForm(request.POST, request.FILES)
        action = request.POST.get("action", "preview")
        if form.is_valid():
            selected_customer = form.cleaned_data["customer"]
            preview = Payment.allocation_preview(
                selected_customer,
                form.cleaned_data["payment_date"],
                form.cleaned_data["amount"],
            )
            if action == "preview" and form.cleaned_data.get("attachment_file"):
                messages.info(
                    request,
                    "If you preview first, choose the attachment file again before saving the payment.",
                )
            if action in {"save", "save_new"}:
                try:
                    payment = Payment.objects.create(
                        customer=selected_customer,
                        payment_date=form.cleaned_data["payment_date"],
                        amount=form.cleaned_data["amount"],
                        method=form.cleaned_data["method"],
                        reference_number=form.cleaned_data["reference_number"],
                        note=form.cleaned_data["note"],
                    )
                except ValidationError as exc:
                    for field, errors in exc.message_dict.items():
                        if field == "__all__":
                            for error in errors:
                                form.add_error(None, error)
                        else:
                            for error in errors:
                                form.add_error(field, error)
                else:
                    upload = form.cleaned_data.get("attachment_file")
                    if upload:
                        try:
                            _save_payment_attachment(payment, upload)
                        except ValidationError as exc:
                            messages.warning(request, str(exc))
                        else:
                            messages.success(request, "Payment saved and attachment uploaded.")
                    else:
                        messages.success(request, "Payment saved successfully.")
                    if action == "save":
                        return redirect(reverse("portal:customer_list"))
                    return redirect(
                        f'{reverse("portal:quick_payment")}?payment_date={payment.payment_date:%Y-%m-%d}&method={payment.method}&focus=amount'
                    )
    else:
        form = PortalQuickPaymentForm(initial=initial)
        if form.initial.get("customer"):
            selected_customer = Customer.objects.filter(pk=form.initial["customer"]).first()

    saved_payment_id = request.GET.get("saved_payment")
    if saved_payment_id and saved_payment_id.isdigit():
        saved_payment = Payment.objects.filter(pk=saved_payment_id).select_related("customer").first()
        if saved_payment:
            selected_customer = saved_payment.customer

    customer_summary = _customer_summary(selected_customer)
    context = _portal_context(
        request,
        active_nav="payments",
        title="Quick Payment",
        form=form,
        selected_customer=selected_customer,
        customer_summary=customer_summary,
        preview=preview,
        action=action,
        saved_payment=saved_payment,
        saved_receipt_pdf_url=reverse("portal:payment_receipt_pdf", args=[saved_payment.pk]) if saved_payment else "",
        saved_receipt_print_url=reverse("portal:payment_receipt_print", args=[saved_payment.pk]) if saved_payment else "",
        saved_attachment_url=reverse("portal:payment_attachment", args=[saved_payment.pk]) if saved_payment else "",
        saved_scan_file_url=reverse("portal:payment_attachment", args=[saved_payment.pk]) if saved_payment and saved_payment.scanned_check_path else "",
        customer_statement_url=f'{reverse("portal:customer_statement")}?customer={selected_customer.pk}' if selected_customer else "",
    )
    return render(request, "portal/quick_payment.html", context)


@login_required(login_url="portal:login")
def payment_attachment_view(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related("customer"), pk=payment_id)
    if request.method == "POST":
        upload = request.FILES.get("attachment_file")
        if not upload:
            messages.error(request, "Choose a file to attach.")
            return redirect(f'{reverse("portal:quick_payment")}?saved_payment={payment.pk}')
        try:
            saved_path = _save_payment_attachment(payment, upload)
        except ValidationError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Attachment saved to {saved_path}.")
        return redirect(f'{reverse("portal:quick_payment")}?saved_payment={payment.pk}')

    if not payment.scanned_check_path:
        raise Http404("Attachment not found.")
    path = Path(payment.scanned_check_path)
    if not path.exists() or not path.is_file():
        raise Http404("Attachment file is missing from disk.")
    return FileResponse(path.open("rb"), content_type="application/octet-stream")


@login_required(login_url="portal:login")
def payment_receipt_pdf_view(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related("customer"), pk=payment_id)
    pdf_bytes = _render_receipt_pdf_bytes(payment)
    if not pdf_bytes:
        messages.error(request, "Receipt PDF generation failed.")
        return redirect(f'{reverse("portal:quick_payment")}?saved_payment={payment.pk}')
    return _pdf_response(pdf_bytes, f"PAY-{payment.pk:05d}.pdf", as_attachment=True)


@login_required(login_url="portal:login")
def payment_receipt_print_view(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related("customer"), pk=payment_id)
    pdf_bytes = _render_receipt_pdf_bytes(payment)
    if not pdf_bytes:
        messages.error(request, "Receipt PDF generation failed.")
        return redirect(f'{reverse("portal:quick_payment")}?saved_payment={payment.pk}')
    return _pdf_response(pdf_bytes, f"PAY-{payment.pk:05d}.pdf", as_attachment=False)


@login_required(login_url="portal:login")
def invoice_list_view(request):
    query = (request.GET.get("q") or "").strip()
    latest_only = request.GET.get("latest", "1") != "0"
    invoices = Invoice.objects.select_related("customer").exclude(status=Invoice.STATUS_VOID).order_by("-period_start", "-id")
    if query:
        invoices = invoices.filter(
            customer__name__icontains=query
        ) | invoices.filter(
            customer__account_number__icontains=query
        ) | invoices.filter(
            invoice_number__icontains=query
        )
        invoices = invoices.select_related("customer").order_by("-period_start", "-id")

    if latest_only:
        latest_ids = Invoice.objects.exclude(status=Invoice.STATUS_VOID).values("customer_id").annotate(max_period=Max("period_start"))
        id_pairs = {(row["customer_id"], row["max_period"]) for row in latest_ids}
        invoices = [invoice for invoice in invoices if (invoice.customer_id, invoice.period_start) in id_pairs]
    else:
        invoices = list(invoices[:200])

    context = _portal_context(
        request,
        active_nav="invoices",
        title="Invoice PDFs",
        invoices=invoices,
        query=query,
        latest_only=latest_only,
    )
    return render(request, "portal/invoices.html", context)


@login_required(login_url="portal:login")
def saved_invoice_list_view(request):
    printed_scope = (request.GET.get("printed_scope") or "unprinted").strip().lower()
    if printed_scope not in {"unprinted", "all", "printed"}:
        printed_scope = "unprinted"
    result = list_saved_invoice_pdf_records(
        limit=400,
        marker="CURRENT",
        printed_scope=printed_scope,
    )
    records = _prepare_dispatch_records(result["records"])
    query_string = request.GET.copy()
    return render(
        request,
        "portal/saved_invoices.html",
        _portal_context(
            request,
            active_nav="dispatch",
            title="Invoice Dispatch",
            page_subtitle=f'Server folder: {result["base_folder"]}' if result["base_folder"] else "Set the invoice PDF output folder first.",
            records=records,
            base_folder=result["base_folder"],
            printed_scope=printed_scope,
            merged_pdf_url=f'{reverse("portal:saved_invoice_merged_pdf")}?{query_string.urlencode()}',
            merged_print_url=f'{reverse("portal:saved_invoice_merged_print")}?{query_string.urlencode()}',
        ),
    )


@login_required(login_url="portal:login")
def saved_invoice_file_view(request):
    relative_path = (request.GET.get("path") or "").strip()
    if not relative_path:
        raise Http404("Saved invoice file not specified.")

    base_folder = get_invoice_output_base_folder()
    if not base_folder or not base_folder.exists():
        raise Http404("Saved invoice folder is not configured.")

    candidate = (base_folder / relative_path).resolve()
    try:
        candidate.relative_to(base_folder.resolve())
    except ValueError:
        raise Http404("Invalid saved invoice path.")

    if not candidate.exists() or not candidate.is_file():
        raise Http404("Saved invoice file not found.")

    return FileResponse(candidate.open("rb"), content_type="application/pdf")


def _saved_invoice_filtered_records(request, limit=0):
    printed_scope = (request.GET.get("printed_scope") or "unprinted").strip().lower()
    if printed_scope not in {"unprinted", "all", "printed"}:
        printed_scope = "unprinted"
    return list_saved_invoice_pdf_records(
        limit=limit,
        marker="CURRENT",
        printed_scope=printed_scope,
    )["records"]


def _saved_invoice_selected_batch(request):
    batch_id = (request.GET.get("batch_id") or "").strip()
    if batch_id == "latest":
        return list_saved_invoice_pdf_records(limit=1)["latest_batch"]
    if batch_id.isdigit():
        return InvoiceGenerationBatch.objects.filter(pk=int(batch_id)).first()
    return None


def _mark_visible_batches_printed(records):
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


def _prepare_dispatch_records(records):
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


@login_required(login_url="portal:login")
def saved_invoice_merged_pdf_view(request):
    records = _saved_invoice_filtered_records(request, limit=0)
    pdf_bytes = merge_saved_invoice_pdf_records(records)
    if not pdf_bytes:
        messages.error(request, "No saved invoice PDFs matched the current filter.")
        return redirect(f'{reverse("portal:saved_invoice_list")}?{request.GET.urlencode()}')
    updated_batches = _mark_visible_batches_printed(records)
    if len(updated_batches) == 1:
        messages.success(request, f"{updated_batches[0].label} was marked as printed.")
    elif updated_batches:
        messages.success(request, f"{len(updated_batches)} batches were marked as printed.")
    return _pdf_response(pdf_bytes, "saved-invoices-merged.pdf", as_attachment=True)


@login_required(login_url="portal:login")
def saved_invoice_merged_print_view(request):
    records = _saved_invoice_filtered_records(request, limit=0)
    pdf_bytes = merge_saved_invoice_pdf_records(records)
    if not pdf_bytes:
        messages.error(request, "No saved invoice PDFs matched the current filter.")
        return redirect(f'{reverse("portal:saved_invoice_list")}?{request.GET.urlencode()}')
    updated_batches = _mark_visible_batches_printed(records)
    if len(updated_batches) == 1:
        messages.success(request, f"{updated_batches[0].label} was marked as printed.")
    elif updated_batches:
        messages.success(request, f"{len(updated_batches)} batches were marked as printed.")
    return _pdf_response(pdf_bytes, "saved-invoices-merged.pdf", as_attachment=False)


@login_required(login_url="portal:login")
def saved_invoice_batch_print_toggle_view(request):
    batch_id = request.POST.get("batch_id")
    if not batch_id or not batch_id.isdigit():
        messages.error(request, "Choose a valid batch first.")
        return redirect(f'{reverse("portal:saved_invoice_list")}?{request.GET.urlencode()}')
    batch = get_object_or_404(InvoiceGenerationBatch, pk=int(batch_id))
    batch.is_printed = not batch.is_printed
    batch.printed_at = timezone.now() if batch.is_printed else None
    batch.save(update_fields=["is_printed", "printed_at"])
    if batch.is_printed:
        messages.success(request, f"{batch.label} was marked as printed.")
    else:
        messages.info(request, f"{batch.label} printed status was cleared.")
    query_string = request.POST.get("return_query", "")
    return redirect(f'{reverse("portal:saved_invoice_list")}?{query_string}' if query_string else reverse("portal:saved_invoice_list"))


@login_required(login_url="portal:login")
def invoice_pdf_view(request, invoice_id):
    invoice = get_object_or_404(Invoice.objects.select_related("customer"), pk=invoice_id)
    pdf_bytes = render_invoice_pdf_bytes(invoice)
    if not pdf_bytes:
        raise Http404("Invoice PDF generation failed.")
    return _pdf_response(pdf_bytes, f"{invoice.invoice_number}.pdf", as_attachment=True)


@login_required(login_url="portal:login")
def invoice_print_view(request, invoice_id):
    invoice = get_object_or_404(Invoice.objects.select_related("customer"), pk=invoice_id)
    pdf_bytes = render_invoice_pdf_bytes(invoice)
    if not pdf_bytes:
        raise Http404("Invoice PDF generation failed.")
    return _pdf_response(pdf_bytes, f"{invoice.invoice_number}.pdf", as_attachment=False)


@login_required(login_url="portal:login")
def report_index_view(request):
    report_links = [
        {"title": "A/R by Billing Term (기간별 미수금 현황)", "description": "Open balances grouped by billing term overdue count.", "url": reverse("portal:ar_aging")},
        {"title": "Payment Activity Report (페이먼 보고서)", "description": "Payment activity for a selected date range.", "url": reverse("portal:payments_report")},
        {"title": "Past-Due Customers (연체 고객 현황)", "description": "Customers with overdue invoices and highest overdue term count.", "url": reverse("portal:overdue_customers")},
        {"title": "Upcoming Billing Schedule (인보이스 일정)", "description": "Customers whose invoices are ready now or due soon.", "url": reverse("portal:upcoming_billing")},
        {"title": "Non-Billable Customers (FREE 고객 현황)", "description": "Active customers and services that are on billing hold or marked complimentary.", "url": reverse("portal:non_billable_customers")},
        {"title": "Auto ACH Review (자동 ACH 점검 대상)", "description": "Auto ACH customers whose payments should be reviewed before the next billing issue date.", "url": reverse("portal:auto_ach_review")},
        {"title": "Customer Statement (고객별 명세서)", "description": "Invoice and payment history for a single customer.", "url": reverse("portal:customer_statement")},
    ]
    return render(request, "portal/report_index.html", _portal_context(request, active_nav="reports", title="Reports", report_links=report_links))


@login_required(login_url="portal:login")
def ar_aging_view(request):
    report_date, rows, totals = _build_ar_aging_data()
    page_obj, page_query, page_numbers = _paginate_items(request, rows, 50)
    return render(
        request,
        "portal/ar_aging.html",
        _portal_context(
            request,
            active_nav="reports",
            title="A/R by Billing Term (기간별 미수금 현황)",
            report_date=report_date,
            rows=page_obj.object_list,
            totals=totals,
            page_obj=page_obj,
            page_query=page_query,
            page_numbers=page_numbers,
        ),
    )


@login_required(login_url="portal:login")
def payments_report_view(request):
    today = timezone.localdate()
    date_from = _parse_iso_date(request.GET.get("date_from"), today.replace(day=1))
    date_to = _parse_iso_date(request.GET.get("date_to"), today)
    payments, method_totals, total_amount = _build_payments_report_data(date_from, date_to)
    page_obj, page_query, page_numbers = _paginate_items(request, payments, 50)
    return render(
        request,
        "portal/payments_report.html",
        _portal_context(
            request,
            active_nav="reports",
            title="Payment Activity Report (페이먼 보고서)",
            date_from=date_from,
            date_to=date_to,
            payments=page_obj.object_list,
            method_totals=method_totals,
            total_amount=total_amount,
            page_obj=page_obj,
            page_query=page_query,
            page_numbers=page_numbers,
        ),
    )


@login_required(login_url="portal:login")
def overdue_customers_view(request):
    report_date, rows, totals = _build_overdue_customers_data()
    page_obj, page_query, page_numbers = _paginate_items(request, rows, 50)
    return render(
        request,
        "portal/overdue_customers.html",
        _portal_context(
            request,
            active_nav="reports",
            title="Past-Due Customers (연체 고객 현황)",
            report_date=report_date,
            rows=page_obj.object_list,
            totals=totals,
            page_obj=page_obj,
            page_query=page_query,
            page_numbers=page_numbers,
        ),
    )


@login_required(login_url="portal:login")
def upcoming_billing_view(request):
    report_date, horizon_date, rows, grouped_rows, term_summaries, totals = _build_upcoming_billing_data()
    page_obj, page_query, page_numbers = _paginate_items(request, rows, 50)
    return render(
        request,
        "portal/upcoming_billing.html",
        _portal_context(
            request,
            active_nav="reports",
            title="Upcoming Billing Schedule (인보이스 일정)",
            report_date=report_date,
            horizon_date=horizon_date,
            rows=page_obj.object_list,
            grouped_rows=grouped_rows,
            term_summaries=term_summaries,
            totals=totals,
            page_obj=page_obj,
            page_query=page_query,
            page_numbers=page_numbers,
        ),
    )


def _build_non_billable_customers_data():
    today = timezone.localdate()
    rows = []
    customer_ids = set()
    totals = {
        "customer_count": 0,
        "service_count": 0,
        "non_billable_amount": Decimal("0.00"),
    }

    services = (
        Service.objects.select_related("customer")
        .filter(customer__is_active=True, is_active=True)
        .exclude(billing_status=Service.BILLING_STATUS_BILLABLE)
        .order_by("customer__name", "customer__account_number", "service_name", "id")
    )
    for service in services:
        customer = service.customer
        customer_ids.add(customer.pk)
        rows.append(
            {
                "customer": customer,
                "service": service,
                "billing_status": service.get_billing_status_display(),
                "billing_amount": service.billing_amount,
                "open_balance": customer.open_balance_as_of(today),
            }
        )
        totals["service_count"] += 1
        totals["non_billable_amount"] += service.billing_amount

    totals["customer_count"] = len(customer_ids)
    return today, rows, totals


@login_required(login_url="portal:login")
def non_billable_customers_view(request):
    report_date, rows, totals = _build_non_billable_customers_data()
    page_obj, page_query, page_numbers = _paginate_items(request, rows, 50)
    return render(
        request,
        "portal/non_billable_customers.html",
        _portal_context(
            request,
            active_nav="reports",
            title="Non-Billable Customers (FREE 고객 현황)",
            report_date=report_date,
            rows=page_obj.object_list,
            totals=totals,
            page_obj=page_obj,
            page_query=page_query,
            page_numbers=page_numbers,
        ),
    )


def _build_auto_ach_review_data(scope="review"):
    today = timezone.localdate()
    if scope not in {"review", "all"}:
        scope = "review"
    rows = []
    for customer in _portal_customer_queryset().filter(is_active=True, auto_ach=True).order_by("name", "account_number"):
        review_needed = customer.auto_ach_review_needed(today)
        if scope == "review" and not review_needed:
            continue
        workflow = _customer_workflow_snapshot(customer, today)
        rows.append(
            {
                "customer": customer,
                "review_needed": review_needed,
                "next_issue_date": customer.next_expected_issue_date(),
                "next_billing_period": workflow.get("period") or "-",
                "open_balance": customer.open_balance_as_of(today),
                "statement_url": f'{reverse("portal:customer_statement")}?customer={customer.pk}',
                "quick_payment_url": f'{reverse("portal:quick_payment")}?customer={customer.pk}',
            }
        )
    totals = {
        "customer_count": len(rows),
        "open_balance_total": sum((row["open_balance"] for row in rows), Decimal("0.00")),
    }
    return today, rows, totals


@login_required(login_url="portal:login")
def auto_ach_review_view(request):
    scope = (request.GET.get("scope") or "review").strip().lower()
    if scope not in {"review", "all"}:
        scope = "review"
    report_date, rows, totals = _build_auto_ach_review_data(scope=scope)
    page_obj, page_query, page_numbers = _paginate_items(request, rows, 50)
    return render(
        request,
        "portal/auto_ach_review.html",
        _portal_context(
            request,
            active_nav="reports",
            title="Auto ACH Review (자동 ACH 점검 대상)",
            report_date=report_date,
            scope=scope,
            rows=page_obj.object_list,
            totals=totals,
            page_obj=page_obj,
            page_query=page_query,
            page_numbers=page_numbers,
        ),
    )


@login_required(login_url="portal:login")
def customer_statement_view(request):
    customers = Customer.objects.order_by("name", "account_number")
    customer_id = request.GET.get("customer")
    selected_customer = customers.filter(pk=customer_id).first() if customer_id else None
    invoices = []
    payments = []
    invoice_count = 0
    payment_count = 0
    open_balance = Decimal("0.00")
    last_payment = None
    primary_service = None
    show_all_invoices = request.GET.get("show_all_invoices") == "1"
    show_all_payments = request.GET.get("show_all_payments") == "1"
    invoice_saved_pdf_urls = {}
    if selected_customer:
        today = timezone.localdate()
        invoice_qs = selected_customer.invoices.exclude(status=Invoice.STATUS_VOID).order_by("-period_start", "-id")
        payment_qs = selected_customer.payments.filter(is_voided=False).prefetch_related("allocations__invoice").order_by("-payment_date", "-id")
        primary_service = selected_customer.services.order_by("-is_active", "id").first()
        invoice_count = invoice_qs.count()
        payment_count = payment_qs.count()
        invoices = list(invoice_qs if show_all_invoices else invoice_qs[:25])
        payments = list(payment_qs if show_all_payments else payment_qs[:25])
        open_balance = selected_customer.open_balance_as_of(today)
        last_payment = payment_qs.first()
        saved_qs = (
            SavedInvoicePDF.objects.filter(customer=selected_customer)
            .exclude(absolute_path="")
            .order_by("invoice_id", "-created_at", "-id")
        )
        seen_invoice_ids = set()
        for saved in saved_qs:
            if not saved.invoice_id or saved.invoice_id in seen_invoice_ids:
                continue
            seen_invoice_ids.add(saved.invoice_id)
            relative_path = saved.relative_path.replace("\\", "/")
            invoice_saved_pdf_urls[saved.invoice_id] = f'{reverse("portal:saved_invoice_file")}?path={relative_path}'
        for invoice in invoices:
            invoice.saved_pdf_url = invoice_saved_pdf_urls.get(invoice.id, "")
            invoice.generated_pdf_url = reverse("portal:invoice_print", args=[invoice.id])
    return render(
        request,
        "portal/customer_statement.html",
        _portal_context(
            request,
            active_nav="reports",
            title="Customer Statement (고객별 명세서)",
            customers=customers,
            selected_customer=selected_customer,
            selected_customer_id=int(customer_id) if customer_id and customer_id.isdigit() else None,
            invoices=invoices,
            payments=payments,
            invoice_count=invoice_count,
            payment_count=payment_count,
            show_all_invoices=show_all_invoices,
            show_all_payments=show_all_payments,
            open_balance=open_balance,
            last_payment=last_payment,
            primary_service=primary_service,
        ),
    )




