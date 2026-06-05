from django.db import models
from django.contrib.auth.models import User


class General(models.Model):
    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("basic_access",        "Puede ver Estadísticas y Mi Dashboard"),
            ("corp_finance_access", "Puede ver Finanzas Corp"),
            ("moon_tax_access",     "Puede ver Tax Lunas"),
            ("moon_tax_admin",      "Puede gestionar tax de lunas"),
        )


class TrackedCorporation(models.Model):
    corporation_id   = models.PositiveIntegerField(unique=True, help_text="EVE Online Corporation ID")
    corporation_name = models.CharField(max_length=100, help_text="Nombre descriptivo")
    is_active        = models.BooleanField(default=True)

    class Meta:
        verbose_name        = "Corp rastreada"
        verbose_name_plural = "Corps rastreadas"
        ordering            = ["corporation_name"]

    def __str__(self):
        return f"{self.corporation_name} ({self.corporation_id})"


class MoonTaxConfig(models.Model):
    tag               = models.CharField(max_length=100, default="default", unique=True)
    ubiquitous_rate   = models.DecimalField(max_digits=5, decimal_places=2, default=5.00)
    common_rate       = models.DecimalField(max_digits=5, decimal_places=2, default=8.00)
    uncommon_rate     = models.DecimalField(max_digits=5, decimal_places=2, default=12.00)
    rare_rate         = models.DecimalField(max_digits=5, decimal_places=2, default=18.00)
    exceptional_rate  = models.DecimalField(max_digits=5, decimal_places=2, default=25.00)
    is_active         = models.BooleanField(default=True)
    updated_at        = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "Configuración de tax lunar"
        verbose_name_plural = "Configuración de tax lunar"

    def __str__(self):
        return (f"{self.tag} — U:{self.ubiquitous_rate}% C:{self.common_rate}% "
                f"UC:{self.uncommon_rate}% R:{self.rare_rate}% E:{self.exceptional_rate}%")

    @property
    def rates_by_group(self):
        return {
            1884: float(self.ubiquitous_rate)  / 100,
            1920: float(self.common_rate)      / 100,
            1921: float(self.uncommon_rate)    / 100,
            1922: float(self.rare_rate)        / 100,
            1923: float(self.exceptional_rate) / 100,
        }


class MoonTaxPayment(models.Model):
    character_id   = models.IntegerField(db_index=True)
    character_name = models.CharField(max_length=100)
    period         = models.CharField(max_length=7, db_index=True)
    isk_owed       = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    is_paid        = models.BooleanField(default=False, db_index=True)
    paid_by        = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="moon_tax_payments_approved"
    )
    paid_at        = models.DateTimeField(null=True, blank=True)
    notes          = models.TextField(blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "Tax lunar — pago"
        verbose_name_plural = "Tax lunar — pagos"
        unique_together     = ("character_id", "period")
        ordering            = ["-period", "character_name"]

    def __str__(self):
        estado = "✅ PAGADO" if self.is_paid else "⏳ PENDIENTE"
        return f"{self.character_name} | {self.period} | {self.isk_owed} ISK | {estado}"
