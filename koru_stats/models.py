from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class General(models.Model):
    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("basic_access",        "Puede ver Estadísticas y Mi Dashboard"),
            ("corp_finance_access", "Puede ver Finanzas Corp"),
            ("moon_tax_access",     "Puede ver Tax Lunas"),
            ("moon_tax_admin",      "Puede gestionar tax de lunas"),
            ("pvp_access",          "Puede ver estadísticas PvP detalladas"),
            ("fc_access",           "Puede ver panel FC/Director"),
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


# ---------------------------------------------------------------------------
# Tablas resumen — pre-agregadas por Celery, consultadas por las vistas
# ---------------------------------------------------------------------------

class CharacterMonthlySummary(models.Model):
    """
    Totales por personaje principal por mes.
    Populated by: tasks.aggregate_monthly_summary (Celery)
    """
    main_character_id   = models.IntegerField(db_index=True)
    main_character_name = models.CharField(max_length=100)
    corporation_id      = models.IntegerField(db_index=True, default=0)
    period              = models.CharField(max_length=7, db_index=True)

    # Minería
    mining_units             = models.BigIntegerField(default=0)
    mining_m3                = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    mining_isk               = models.DecimalField(max_digits=20, decimal_places=2, default=0,
                                                   help_text="ISK a precio raw (market sell)")
    mining_isk_compressed    = models.DecimalField(max_digits=20, decimal_places=2, default=0,
                                                   help_text="ISK si se vendiera comprimido")
    mining_isk_reprocessed   = models.DecimalField(max_digits=20, decimal_places=2, default=0,
                                                   help_text="ISK si se reprocesara al 85%")

    # Wallet
    bounty_isk = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    ess_isk    = models.DecimalField(max_digits=20, decimal_places=2, default=0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "Resumen mensual por personaje"
        verbose_name_plural = "Resúmenes mensuales por personaje"
        constraints = [
            models.UniqueConstraint(
                fields=["main_character_id", "period"],
                name="unique_char_monthly_summary",
            ),
        ]
        indexes = [
            models.Index(fields=["period", "corporation_id"], name="koru_cms_period_corp"),
            models.Index(fields=["period", "mining_isk"],     name="koru_cms_period_mining"),
            models.Index(fields=["period", "bounty_isk"],     name="koru_cms_period_bounty"),
        ]

    def __str__(self):
        return f"{self.main_character_name} | {self.period}"

    @property
    def total_isk(self):
        return self.mining_isk + self.bounty_isk + self.ess_isk


class CharacterMonthlyOre(models.Model):
    """
    Desglose de tipos de ore por personaje principal por mes.
    Populated by: tasks.aggregate_monthly_ore (Celery)
    """
    main_character_id = models.IntegerField(db_index=True)
    corporation_id    = models.IntegerField(db_index=True, default=0)
    period            = models.CharField(max_length=7, db_index=True)

    type_id   = models.IntegerField()
    type_name = models.CharField(max_length=100)
    quantity  = models.BigIntegerField(default=0)
    m3        = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    isk             = models.DecimalField(max_digits=20, decimal_places=2, default=0,
                                          help_text="ISK a precio raw (market sell)")
    isk_compressed  = models.DecimalField(max_digits=20, decimal_places=2, default=0,
                                          help_text="ISK si se vendiera comprimido")
    isk_reprocessed = models.DecimalField(max_digits=20, decimal_places=2, default=0,
                                          help_text="ISK si se reprocesara al 85%")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "Ore mensual por personaje"
        verbose_name_plural = "Ore mensuales por personaje"
        constraints = [
            models.UniqueConstraint(
                fields=["main_character_id", "period", "type_id"],
                name="unique_char_monthly_ore",
            ),
        ]
        indexes = [
            models.Index(fields=["period", "corporation_id", "type_id"], name="koru_cmo_period_corp_type"),
        ]

    def __str__(self):
        return f"{self.main_character_id} | {self.period} | {self.type_name}"


# ---------------------------------------------------------------------------
# Precios de ore — actualizados periódicamente desde Fuzzwork Market API
# ---------------------------------------------------------------------------

class OreMarketPrice(models.Model):
    """
    Precio de mercado de cada tipo de ore, en tres modalidades de valoración.
    Todos los precios son ISK por unidad de ore SIN comprimir (normalizados).
    Populated by: tasks.update_ore_prices (Celery, diario)
    """
    type_id   = models.IntegerField(unique=True, db_index=True)
    type_name = models.CharField(max_length=100)

    price_raw          = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    price_compressed   = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    price_reprocessed  = models.DecimalField(max_digits=20, decimal_places=4, default=0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "Precio de ore"
        verbose_name_plural = "Precios de ore"
        ordering            = ["type_name"]

    def __str__(self):
        return f"{self.type_name} — raw:{self.price_raw} | comp:{self.price_compressed} | repr:{self.price_reprocessed}"


# ---------------------------------------------------------------------------
# PvP mensual — pre-agregado desde aastatistics_zkillmonth
# ---------------------------------------------------------------------------

class CharacterMonthlyPvp(models.Model):
    """
    Estadísticas PvP mensuales por personaje principal.
    Fuente: aastatistics_zkillmonth (zKill API)
    Populated by: tasks.aggregate_character_monthly_pvp (Celery)
    """
    main_character_id   = models.IntegerField(db_index=True)
    main_character_name = models.CharField(max_length=100)
    corporation_id      = models.IntegerField(db_index=True, default=0)
    period              = models.CharField(max_length=7, db_index=True)

    ships_destroyed = models.IntegerField(default=0)
    ships_lost      = models.IntegerField(default=0)
    isk_destroyed   = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    isk_lost        = models.DecimalField(max_digits=20, decimal_places=2, default=0)

    # Detalle de participación (desde ESI attackers[])
    final_blows     = models.IntegerField(default=0)   # tiros de gracia
    participations  = models.IntegerField(default=0)   # killmails donde aparece como atacante
    solo_kills      = models.IntegerField(default=0)   # kills en solitario (zkb.solo)
    top_damage_kills = models.IntegerField(default=0)  # kills donde hizo más daño (sin ser final blow)
    damage_dealt    = models.BigIntegerField(default=0) # daño total infligido

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "PvP mensual por personaje"
        verbose_name_plural = "PvP mensuales por personaje"
        constraints = [
            models.UniqueConstraint(
                fields=["main_character_id", "period"],
                name="unique_char_monthly_pvp",
            ),
        ]
        indexes = [
            models.Index(fields=["period", "corporation_id"],  name="koru_pvp_period_corp"),
            models.Index(fields=["period", "isk_destroyed"],   name="koru_pvp_period_isk"),
            models.Index(fields=["period", "ships_destroyed"], name="koru_pvp_period_ships"),
        ]

    def __str__(self):
        return f"{self.main_character_name} | {self.period} | {self.ships_destroyed}K/{self.ships_lost}D"

    @property
    def isk_efficiency(self):
        total = self.isk_destroyed + self.isk_lost
        if not total:
            return 0.0
        return float(self.isk_destroyed / total * 100)

    @property
    def kd_ratio(self):
        if not self.ships_lost:
            return float(self.ships_destroyed)
        return float(self.ships_destroyed / self.ships_lost)


class CharacterKillRecord(models.Model):
    """Registro individual de killmail — kills y losses por personaje."""

    main_character_id   = models.BigIntegerField(db_index=True)
    main_character_name = models.CharField(max_length=255, default="")
    killmail_id         = models.BigIntegerField()
    period              = models.CharField(max_length=7, db_index=True)  # "2026-05"
    is_loss             = models.BooleanField(default=False)
    ship_type_id        = models.IntegerField(default=0)   # nave víctima (kills) o nave propia (losses)
    value_isk           = models.FloatField(default=0.0)
    kill_date           = models.DateField(null=True, blank=True)
    final_blow          = models.BooleanField(default=False)
    solo                = models.BooleanField(default=False)

    class Meta:
        verbose_name        = "Registro killmail"
        verbose_name_plural = "Registros killmail"
        unique_together = ("main_character_id", "killmail_id")
        indexes = [
            models.Index(fields=["main_character_id", "period", "is_loss"],
                         name="koru_killrec_char_period_loss"),
        ]

    def __str__(self):
        t = "Loss" if self.is_loss else "Kill"
        return f"{self.main_character_name} | {t} | {self.killmail_id}"

    @property
    def zkill_url(self):
        return f"https://zkillboard.com/kill/{self.killmail_id}/"

    @property
    def ship_image_url(self):
        return f"https://images.evetech.net/types/{self.ship_type_id}/render?size=64"
