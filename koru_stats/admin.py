from django import forms
from django.contrib import admin
from django.utils import timezone
from allianceauth.eveonline.models import EveCorporationInfo

from .models import TrackedCorporation, MoonTaxConfig, MoonTaxPayment


# ---------------------------------------------------------------------------
# TrackedCorporation
# ---------------------------------------------------------------------------

def _corps_con_miembros():
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT DISTINCT ci.corporation_id, ci.corporation_name
            FROM eveonline_evecorporationinfo ci
            JOIN eveonline_evecharacter ec ON ec.corporation_id = ci.corporation_id
            JOIN authentication_characterownership co ON co.character_id = ec.id
            ORDER BY ci.corporation_name
        """)
        return [(row[0], f"{row[1]} ({row[0]})") for row in cursor.fetchall()]


class TrackedCorporationForm(forms.ModelForm):
    corp_choice = forms.ChoiceField(
        choices=[],
        label="Corporación",
        required=True,
        help_text="Solo corps con personajes registrados en Auth"
    )

    class Meta:
        model  = TrackedCorporation
        fields = ["is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "---------")] + _corps_con_miembros()
        self.fields["corp_choice"].choices = choices
        if self.instance and self.instance.pk:
            self.fields["corp_choice"].initial = self.instance.corporation_id

    def clean(self):
        cleaned = super().clean()
        corp_id = cleaned.get("corp_choice")
        if corp_id:
            try:
                corp = EveCorporationInfo.objects.get(corporation_id=corp_id)
                cleaned["_corporation_id"]   = corp.corporation_id
                cleaned["_corporation_name"] = corp.corporation_name
            except EveCorporationInfo.DoesNotExist:
                raise forms.ValidationError("Corp no encontrada")
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.corporation_id   = self.cleaned_data["_corporation_id"]
        instance.corporation_name = self.cleaned_data["_corporation_name"]
        if commit:
            instance.save()
        return instance


@admin.register(TrackedCorporation)
class TrackedCorporationAdmin(admin.ModelAdmin):
    form          = TrackedCorporationForm
    list_display  = ("corporation_name", "corporation_id", "is_active")
    list_editable = ("is_active",)
    ordering      = ("corporation_name",)


# ---------------------------------------------------------------------------
# MoonTaxConfig
# ---------------------------------------------------------------------------

@admin.register(MoonTaxConfig)
class MoonTaxConfigAdmin(admin.ModelAdmin):
    list_display = ("tag", "ubiquitous_rate", "common_rate", "uncommon_rate", "rare_rate", "exceptional_rate", "is_active", "updated_at")
    list_editable = ("ubiquitous_rate", "common_rate", "uncommon_rate", "rare_rate", "exceptional_rate", "is_active")
    readonly_fields = ("updated_at",)

    fieldsets = (
        ("Identificación", {
            "fields": ("tag", "is_active", "updated_at")
        }),
        ("Tasas por tier (%)", {
            "description": "Porcentaje sobre el valor de mercado del ore minado.",
            "fields": (
                ("ubiquitous_rate", "common_rate"),
                ("uncommon_rate",   "rare_rate"),
                ("exceptional_rate",),
            )
        }),
    )


# ---------------------------------------------------------------------------
# MoonTaxPayment
# ---------------------------------------------------------------------------

@admin.register(MoonTaxPayment)
class MoonTaxPaymentAdmin(admin.ModelAdmin):
    list_display  = ("character_name", "period", "isk_owed_display", "is_paid", "paid_by", "paid_at")
    list_filter   = ("is_paid", "period")
    search_fields = ("character_name", "period")
    readonly_fields = ("character_id", "character_name", "period", "isk_owed", "created_at", "updated_at", "paid_by", "paid_at")
    ordering = ("-period", "character_name")

    fieldsets = (
        ("Piloto", {
            "fields": ("character_name", "character_id", "period", "isk_owed")
        }),
        ("Estado del pago", {
            "fields": ("is_paid", "paid_by", "paid_at", "notes")
        }),
        ("Auditoría", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",)
        }),
    )

    def isk_owed_display(self, obj):
        v = float(obj.isk_owed)
        if v >= 1e9:
            return f"{v/1e9:.2f} B ISK"
        if v >= 1e6:
            return f"{v/1e6:.2f} M ISK"
        return f"{v:,.0f} ISK"
    isk_owed_display.short_description = "ISK owed"

    def has_add_permission(self, request):
        return False  # Solo se crean desde la vista, no manualmente
