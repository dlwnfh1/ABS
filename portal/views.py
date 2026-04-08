from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Max
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
    render_invoice_pdf_bytes,
)
from customers.models import Customer
from payments.models import Payment
from reports.models import InvoiceGenerationBatch, SystemSetting

from .forms import PortalQuickPaymentForm


def _portal_context(request, **extra):
    return {
        "nav_items": [
            {"label": "Customers", "url": reverse("portal:customer_list"), "key": "customers"},
            {"label": "Quick Payment", "url": reverse("portal:quick_payment"), "key": "payments"},
            {"label": "Invoice Dispatch", "url": reverse("portal:saved_invoice_list"), "key": "dispatch"},
            {"label": "Reports", "url": reverse("portal:report_index"), "key": "reports"},
        ],
        **extra,
    }


def _parse_iso_date(value, fallback):
    if not value:
        return fallback
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return fallback


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
    latest_payment = customer.payments.filter(is_voided=False).order_by("-payment_date", "-id").first()
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
    for customer in Customer.objects.filter(is_active=True).order_by("name", "account_number"):
        invoices = customer.invoices.exclude(status=Invoice.STATUS_VOID).order_by("period_start", "id")
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
    for customer in Customer.objects.filter(is_active=True).order_by("name", "account_number"):
        overdue_entries = []
        for invoice in customer.invoices.exclude(status=Invoice.STATUS_VOID).order_by("due_date", "period_start", "id"):
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

    for customer in Customer.objects.filter(is_active=True).order_by("name", "account_number"):
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
    latest_invoice = customer.invoices.exclude(status=Invoice.STATUS_VOID).order_by("-period_start", "-id").first()
    if latest_invoice:
        period_start = latest_invoice.next_period_start
        period_end = latest_invoice.next_period_end
        issue_date = period_start - timedelta(days=15)
        existing_invoice = Invoice.objects.filter(customer=customer, period_start=period_start, period_end=period_end).first()
        if existing_invoice:
            status = "Already Issued"
        else:
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
    customers = Customer.objects.filter(is_active=True).order_by("name", "account_number")
    if query:
        customers = customers.filter(
            name__icontains=query
        ) | customers.filter(
            account_number__icontains=query
        ) | customers.filter(
            billing_address1__icontains=query
        ) | customers.filter(
            billing_address2__icontains=query
        )
        customers = customers.order_by("name", "account_number")

    today = timezone.localdate()
    rows = []
    for customer in customers[:300]:
        workflow = _customer_workflow_snapshot(customer, today)
        last_payment = customer.payments.filter(is_voided=False).order_by("-payment_date", "-id").first()
        rows.append(
            {
                "customer": customer,
                "open_balance": customer.open_balance_as_of(today),
                "workflow_status": workflow["status"],
                "next_billing_period": workflow["period"] or "-",
                "last_payment": last_payment,
                "statement_url": f'{reverse("portal:customer_statement")}?customer={customer.pk}',
                "saved_pdf_url": f'{reverse("portal:saved_invoice_list")}?account_number={customer.account_number}',
                "quick_payment_url": f'{reverse("portal:quick_payment")}?customer={customer.pk}',
            }
        )

    return render(
        request,
        "portal/customers.html",
        _portal_context(
            request,
            active_nav="customers",
            title="Customers",
            query=query,
            rows=rows,
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
                        return redirect(
                            f'{reverse("portal:quick_payment")}?saved_payment={payment.pk}&payment_date={payment.payment_date:%Y-%m-%d}&method={payment.method}'
                        )
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
    query = (request.GET.get("q") or "").strip()
    latest_only = request.GET.get("latest", "0") == "1"
    account_number = (request.GET.get("account_number") or "").strip()
    marker = request.GET.get("marker", "CURRENT")
    marker = marker.strip().upper()
    if marker not in {"", "CURRENT"}:
        marker = "CURRENT"
    printed_scope = (request.GET.get("printed_scope") or "unprinted").strip().lower()
    if printed_scope not in {"unprinted", "all", "printed"}:
        printed_scope = "unprinted"
    batch_id = request.GET.get("batch_id", "").strip()
    latest_unprinted_batch = InvoiceGenerationBatch.objects.filter(is_printed=False).order_by("-created_at", "-id").first()
    if batch_id == "latest":
        batch_id = ""
        latest_batch = list_saved_invoice_pdf_records(limit=1)["latest_batch"]
        if latest_batch:
            batch_id = str(latest_batch.pk)
    today = timezone.localdate()
    date_from = _parse_iso_date(request.GET.get("date_from"), today - timedelta(days=30))
    date_to = _parse_iso_date(request.GET.get("date_to"), today)
    result = list_saved_invoice_pdf_records(
        query=query,
        account_number=account_number,
        latest_only=latest_only,
        limit=400,
        date_from=date_from,
        date_to=date_to,
        marker=marker or None,
        batch_id=int(batch_id) if batch_id.isdigit() else None,
        printed_scope=printed_scope,
    )
    base_folder = result["base_folder"]
    query_string = request.GET.copy()
    if batch_id and "batch_id" not in query_string:
        query_string["batch_id"] = batch_id
    return render(
        request,
        "portal/saved_invoices.html",
        _portal_context(
            request,
            active_nav="dispatch",
            title="Invoice Dispatch",
            page_subtitle=f"Server folder: {base_folder}" if base_folder else "Set the invoice PDF output folder first.",
            records=result["records"],
            query=query,
            latest_only=latest_only,
            account_number=account_number,
            base_folder=base_folder,
            date_from=date_from,
            date_to=date_to,
            marker=marker,
            printed_scope=printed_scope,
            batch_id=batch_id,
            recent_batches=result["recent_batches"],
            latest_batch=result["latest_batch"],
            latest_unprinted_batch=latest_unprinted_batch,
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
    query = (request.GET.get("q") or "").strip()
    latest_only = request.GET.get("latest", "0") == "1"
    account_number = (request.GET.get("account_number") or "").strip()
    marker = request.GET.get("marker", "CURRENT")
    marker = marker.strip().upper()
    if marker not in {"", "CURRENT"}:
        marker = "CURRENT"
    printed_scope = (request.GET.get("printed_scope") or "unprinted").strip().lower()
    if printed_scope not in {"unprinted", "all", "printed"}:
        printed_scope = "unprinted"
    batch_id = request.GET.get("batch_id", "").strip()
    if batch_id == "latest":
        batch_id = ""
        latest_batch = list_saved_invoice_pdf_records(limit=1)["latest_batch"]
        if latest_batch:
            batch_id = str(latest_batch.pk)
    today = timezone.localdate()
    date_from = _parse_iso_date(request.GET.get("date_from"), today - timedelta(days=30))
    date_to = _parse_iso_date(request.GET.get("date_to"), today)
    return list_saved_invoice_pdf_records(
        query=query,
        account_number=account_number,
        latest_only=latest_only,
        limit=limit,
        date_from=date_from,
        date_to=date_to,
        marker=marker or None,
        batch_id=int(batch_id) if batch_id.isdigit() else None,
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


@login_required(login_url="portal:login")
def saved_invoice_merged_pdf_view(request):
    records = _saved_invoice_filtered_records(request, limit=0)
    pdf_bytes = merge_saved_invoice_pdf_records(records)
    if not pdf_bytes:
        messages.error(request, "No saved invoice PDFs matched the current filter.")
        return redirect(f'{reverse("portal:saved_invoice_list")}?{request.GET.urlencode()}')
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
        {"title": "A/R by Billing Term", "description": "Open balances grouped by billing term overdue count.", "url": reverse("portal:ar_aging")},
        {"title": "Payment Activity Report", "description": "Payment activity for a selected date range.", "url": reverse("portal:payments_report")},
        {"title": "Past-Due Customers", "description": "Customers with overdue invoices and highest overdue term count.", "url": reverse("portal:overdue_customers")},
        {"title": "Upcoming Billing Schedule", "description": "Customers whose invoices are ready now or due soon.", "url": reverse("portal:upcoming_billing")},
        {"title": "Customer Statement", "description": "Invoice and payment history for a single customer.", "url": reverse("portal:customer_statement")},
    ]
    return render(request, "portal/report_index.html", _portal_context(request, active_nav="reports", title="Reports", report_links=report_links))


@login_required(login_url="portal:login")
def ar_aging_view(request):
    report_date, rows, totals = _build_ar_aging_data()
    return render(
        request,
        "portal/ar_aging.html",
        _portal_context(request, active_nav="reports", title="A/R by Billing Term", report_date=report_date, rows=rows, totals=totals),
    )


@login_required(login_url="portal:login")
def payments_report_view(request):
    today = timezone.localdate()
    date_from = _parse_iso_date(request.GET.get("date_from"), today.replace(day=1))
    date_to = _parse_iso_date(request.GET.get("date_to"), today)
    payments, method_totals, total_amount = _build_payments_report_data(date_from, date_to)
    return render(
        request,
        "portal/payments_report.html",
        _portal_context(
            request,
            active_nav="reports",
            title="Payment Activity Report",
            date_from=date_from,
            date_to=date_to,
            payments=payments,
            method_totals=method_totals,
            total_amount=total_amount,
        ),
    )


@login_required(login_url="portal:login")
def overdue_customers_view(request):
    report_date, rows, totals = _build_overdue_customers_data()
    return render(
        request,
        "portal/overdue_customers.html",
        _portal_context(request, active_nav="reports", title="Past-Due Customers", report_date=report_date, rows=rows, totals=totals),
    )


@login_required(login_url="portal:login")
def upcoming_billing_view(request):
    report_date, horizon_date, rows, grouped_rows, term_summaries, totals = _build_upcoming_billing_data()
    return render(
        request,
        "portal/upcoming_billing.html",
        _portal_context(
            request,
            active_nav="reports",
            title="Upcoming Billing Schedule",
            report_date=report_date,
            horizon_date=horizon_date,
            rows=rows,
            grouped_rows=grouped_rows,
            term_summaries=term_summaries,
            totals=totals,
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
    show_all_invoices = request.GET.get("show_all_invoices") == "1"
    show_all_payments = request.GET.get("show_all_payments") == "1"
    if selected_customer:
        today = timezone.localdate()
        invoice_qs = selected_customer.invoices.exclude(status=Invoice.STATUS_VOID).order_by("-period_start", "-id")
        payment_qs = selected_customer.payments.filter(is_voided=False).prefetch_related("allocations__invoice").order_by("-payment_date", "-id")
        invoice_count = invoice_qs.count()
        payment_count = payment_qs.count()
        invoices = list(invoice_qs if show_all_invoices else invoice_qs[:25])
        payments = list(payment_qs if show_all_payments else payment_qs[:25])
        open_balance = selected_customer.open_balance_as_of(today)
        last_payment = payment_qs.first()
    return render(
        request,
        "portal/customer_statement.html",
        _portal_context(
            request,
            active_nav="reports",
            title="Customer Statement",
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
        ),
    )
