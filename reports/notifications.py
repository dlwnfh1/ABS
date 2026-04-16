from django.conf import settings
from django.core.mail import send_mail
from typing import List


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
