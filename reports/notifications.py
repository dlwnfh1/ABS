from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from django.conf import settings
from django.core.mail import EmailMessage, send_mail


def _send_simple_email(subject: str, body: str, recipients: List[str]) -> int:
    clean_recipients = [email for email in recipients if email]
    if not clean_recipients:
        return 0
    return send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=clean_recipients,
        fail_silently=False,
    )


def send_ready_customer_summary(ready_count: int) -> int:
    if ready_count <= 0:
        return 0
    subject = "ABS Ready Customers Alert"
    body = f"현재 Ready 고객은 {ready_count}명입니다."
    return _send_simple_email(subject, body, settings.ABS_ADMIN_ALERT_EMAILS)


def send_billing_dispatch_alert(customer_count: int) -> int:
    if customer_count <= 0:
        return 0
    subject = "ABS Invoice Dispatch Ready"
    body = (
        f"{customer_count}명의 고객에게 발송할 인보이스가 생성되었습니다. "
        "Invoice Dispatch에서 프린트 후 발송해 주세요."
    )
    return _send_simple_email(subject, body, settings.ABS_BILLING_ALERT_EMAILS)


def send_saved_invoice_emails(records) -> Dict[str, object]:
    grouped = defaultdict(lambda: {"records": [], "to": [], "cc": []})
    missing_email_customers = set()
    skipped_customers = set()

    for record in records:
        customer = record.get("customer")
        if not customer:
            continue
        if customer.invoice_delivery_method == "none":
            skipped_customers.add(customer.pk)
            continue
        if customer.invoice_delivery_method not in {"email", "both"}:
            continue
        to_emails = _normalized_email_list(customer.invoice_email_to)
        cc_emails = _normalized_email_list(customer.invoice_email_cc)
        if not to_emails:
            missing_email_customers.add(customer.pk)
            continue
        path = record.get("path")
        if not path or not Path(path).exists():
            continue
        group_key = (tuple(sorted(to_emails)), tuple(sorted(cc_emails)))
        grouped[group_key]["records"].append(record)
        grouped[group_key]["to"] = to_emails
        grouped[group_key]["cc"] = cc_emails

    sent_customers = 0
    sent_invoices = 0
    failed = []

    for grouped_item in grouped.values():
        customer_records = grouped_item["records"]
        customer_names = []
        attachments = []
        for record in customer_records:
            customer = record["customer"]
            if customer and customer.name not in customer_names:
                customer_names.append(customer.name)
            pdf_path = Path(record["path"])
            attachments.append((pdf_path.name, pdf_path.read_bytes(), "application/pdf"))

        subject = "Alarm Monitoring invoice Statement ([네오 시큐리티] 알람 모니터링 청구서)"
        if len(customer_names) == 1:
            body = (
                "Attached invoice statement customer:\n"
                f"- {customer_names[0]}\n\n"
                "Thank you."
            )
        else:
            customer_lines = "\n".join(f"- {name}" for name in customer_names)
            body = (
                "Attached invoice statement customers:\n"
                f"{customer_lines}\n\n"
                "Thank you."
            )
        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=grouped_item["to"],
            cc=grouped_item["cc"],
        )
        for filename, content, mime in attachments:
            email.attach(filename, content, mime)
        try:
            email.send(fail_silently=False)
            sent_customers += len(customer_names)
            sent_invoices += len(customer_records)
        except Exception as exc:
            failed.append(f"{', '.join(customer_names)} ({', '.join(grouped_item['to'])}): {exc}")

    return {
        "sent_customers": sent_customers,
        "sent_invoices": sent_invoices,
        "missing_email_customers": len(missing_email_customers),
        "skipped_customers": len(skipped_customers),
        "failed": failed,
    }


def _normalized_email_list(raw_value: str) -> List[str]:
    seen = set()
    emails = []
    for email in (raw_value or "").split(","):
        normalized = email.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        emails.append(normalized)
    return emails
