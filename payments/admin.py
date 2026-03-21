from django.contrib import admin

from .models import Payment, PaymentAllocation


class PaymentAllocationInline(admin.TabularInline):
    model = PaymentAllocation
    extra = 0
    can_delete = False
    readonly_fields = ("invoice", "amount", "created_at")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("customer", "payment_date", "amount", "applied_amount", "unapplied_amount", "method", "reference_number")
    search_fields = ("customer__name", "customer__account_number", "reference_number")
    autocomplete_fields = ("customer",)
    inlines = [PaymentAllocationInline]
    list_select_related = ("customer",)

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        customer_id = request.GET.get("customer")
        if customer_id:
            initial["customer"] = customer_id
        return initial


@admin.register(PaymentAllocation)
class PaymentAllocationAdmin(admin.ModelAdmin):
    list_display = ("payment", "invoice", "amount", "created_at")
    search_fields = ("payment__customer__name", "payment__customer__account_number", "invoice__invoice_number")
    autocomplete_fields = ("payment", "invoice")
