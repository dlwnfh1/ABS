from django import forms
from django.utils import timezone

from customers.models import Customer, Service, PHONE_NUMBER_VALIDATOR
from payments.models import Payment


class PortalQuickPaymentForm(forms.Form):
    customer = forms.ModelChoiceField(
        queryset=Customer.objects.filter(is_active=True).order_by("name", "account_number"),
        empty_label="Select a customer",
    )
    payment_date = forms.DateField(
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    amount = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0.01)
    method = forms.ChoiceField(choices=Payment.METHOD_CHOICES, initial=Payment.METHOD_CHECK)
    reference_number = forms.CharField(max_length=100, required=False)
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    attachment_file = forms.FileField(
        required=False,
        help_text="Optional. If you preview first, choose the file again before saving.",
    )


class BasePortalCustomerForm(forms.Form):
    name = forms.CharField(max_length=255)
    account_number = forms.CharField(max_length=50)
    billing_address1 = forms.CharField(max_length=255)
    billing_address2 = forms.CharField(max_length=255, required=False)
    email_address = forms.RegexField(
        regex=PHONE_NUMBER_VALIDATOR.regex.pattern,
        required=False,
        label="Phone number",
        error_messages={"invalid": PHONE_NUMBER_VALIDATOR.message},
        widget=forms.TextInput(attrs={"placeholder": "123-456-7890", "inputmode": "tel", "pattern": r"\d{3}-\d{3}-\d{4}"}),
    )
    billing_term = forms.ChoiceField(choices=Customer.BILLING_TERM_CHOICES, initial=3)
    auto_ach = forms.BooleanField(required=False, initial=False)
    tax_rate = forms.DecimalField(max_digits=5, decimal_places=2, initial="0.00")
    first_billing_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    is_active = forms.BooleanField(required=False, initial=True)

    service_name = forms.CharField(max_length=255, initial="Alarm Monitoring Service")
    service_address1 = forms.CharField(max_length=255, required=False)
    service_address2 = forms.CharField(max_length=255, required=False)
    billing_amount = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0)
    service_billing_status = forms.ChoiceField(choices=Service.BILLING_STATUS_CHOICES, initial=Service.BILLING_STATUS_BILLABLE)
    service_is_active = forms.BooleanField(required=False, initial=True)


class PortalCustomerCreateForm(BasePortalCustomerForm):

    def clean_account_number(self):
        account_number = (self.cleaned_data.get("account_number") or "").strip()
        if Customer.objects.filter(account_number=account_number).exists():
            raise forms.ValidationError("This account number already exists.")
        return account_number


class PortalCustomerEditForm(BasePortalCustomerForm):
    def __init__(self, *args, customer=None, **kwargs):
        self.customer = customer
        super().__init__(*args, **kwargs)

    def clean_account_number(self):
        account_number = (self.cleaned_data.get("account_number") or "").strip()
        queryset = Customer.objects.filter(account_number=account_number)
        if self.customer:
            queryset = queryset.exclude(pk=self.customer.pk)
        if queryset.exists():
            raise forms.ValidationError("This account number already exists.")
        return account_number
