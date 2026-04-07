from django import forms
from django.utils import timezone

from customers.models import Customer
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
