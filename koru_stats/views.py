import json
import logging
import calendar
from decimal import Decimal
from datetime import date, datetime

from django.contrib.auth.decorators import permission_required
from django.core.cache import cache
from django.db import connection
from django.db.models import F, Sum
from django.shortcuts import render

from .models import CharacterMonthlyOre, CharacterMonthlySummary, CharacterMonthlyPvp, CharacterKillRecord, TrackedCorporation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def _fetchall(cursor):
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _to_json(data):
    return json.dumps(data, cls=_DecimalEncoder)


def _rango_mes(año, mes):
    inicio = date(año, mes, 1)
    fin = date(año + 1, 1, 1) if mes == 12 else date(año, mes + 1, 1)
    return str(inicio), str(fin)


def _get_periodos():
    periodos = []
    hoy = datetime.now()
    for i in range(36):
        mes  = hoy.month - i
        anio = hoy.year
        while mes <= 0:
            mes  += 12
            anio -= 1
        periodos.append({
            "valor": f"{anio}-{mes:02d}",
            "label": date(anio, mes, 1).strftime("%B %Y"),
            "anio":  anio,
        })
    return periodos


def _get_periodos_con_datos(tipo="general"):
    """Devuelve solo los períodos YYYY-MM que tienen datos reales.

    Resultado cacheado 10 minutos — los períodos disponibles cambian
    como mucho una vez al mes (cuando corptools importa datos nuevos).
    """
    cache_key = f"koru_periodos_{tipo}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        with connection.cursor() as cursor:
            if tipo == "luna":
                cursor.execute("""
                    SELECT DISTINCT DATE_FORMAT(ml.date, '%Y-%m') AS periodo
                    FROM corptools_characterminingledger ml
                    JOIN eve_sde_itemtype it ON it.id = ml.type_name_id
                    JOIN eve_sde_itemgroup ig ON ig.id = it.group_id
                    WHERE ig.id IN (1884, 1920, 1921, 1922, 1923)
                    ORDER BY periodo DESC
                """)
            elif tipo == "corp":
                cursor.execute("""
                    SELECT DISTINCT DATE_FORMAT(date, '%Y-%m') AS periodo
                    FROM corptools_corporationwalletjournalentry
                    WHERE amount > 0
                    ORDER BY periodo DESC
                """)
            elif tipo == "pvp":
                rows = list(
                    CharacterMonthlyPvp.objects
                    .values_list("period", flat=True)
                    .distinct()
                    .order_by("-period")
                )
                cache.set(cache_key, _build_periodos_list(rows), timeout=600)
                return cache.get(cache_key)
            else:
                # Consulta rápida sobre tabla pre-agregada (índice simple vs scan corptools)
                rows = list(
                    CharacterMonthlySummary.objects
                    .values_list("period", flat=True)
                    .distinct()
                    .order_by("-period")
                )
                cache.set(cache_key, _build_periodos_list(rows), timeout=600)
                return cache.get(cache_key)
            rows = [row[0] for row in cursor.fetchall()]
    except Exception:
        rows = []

    periodos = []
    for valor in rows:
        try:
            anio = int(valor[:4])
            mes  = int(valor[5:7])
            periodos.append({
                "valor": valor,
                "label": date(anio, mes, 1).strftime("%B"),
                "anio":  anio,
                "mes":   mes,
            })
        except (ValueError, IndexError):
            continue

    cache.set(cache_key, periodos, timeout=600)  # 10 minutos
    return periodos


def _build_periodos_list(valores):
    """Convierte lista de strings 'YYYY-MM' en la estructura que usa _build_selector_context."""
    periodos = []
    for valor in valores:
        try:
            anio = int(valor[:4])
            mes  = int(valor[5:7])
            periodos.append({"valor": valor, "label": date(anio, mes, 1).strftime("%B"), "anio": anio, "mes": mes})
        except (ValueError, IndexError):
            continue
    return periodos


def _build_selector_context(periodos_datos, periodo_sel, anio_sel):
    """Contexto para el selector de dos niveles año/mes."""
    años = sorted(set(p["anio"] for p in periodos_datos), reverse=True)
    meses_por_anio = {}
    for p in periodos_datos:
        meses_por_anio.setdefault(p["anio"], []).append(p)

    return {
        "años_disponibles":  años,
        "meses_por_anio":    meses_por_anio,
        "anio_sel":          anio_sel,
        "periodo_sel":       periodo_sel,
        "periodos_json":     json.dumps({
            str(anio): [
                {"valor": p["valor"], "label": p["label"]}
                for p in meses
            ]
            for anio, meses in meses_por_anio.items()
        }),
    }


def _parse_periodo(request):
    hoy = datetime.now()
    raw = request.GET.get("periodo", "")
    try:
        anio = int(raw[:4])
        mes  = int(raw[5:7])
        if not (1 <= mes <= 12 and 2020 <= anio <= hoy.year):
            raise ValueError
    except (ValueError, IndexError):
        mes, anio = hoy.month, hoy.year
    inicio, fin = _rango_mes(anio, mes)
    return mes, anio, inicio, fin, f"{anio}-{mes:02d}"


def _get_corp_ids():
    """Lee las corp IDs activas configuradas en el admin.

    Resultado cacheado 5 minutos — cambia solo cuando un admin
    activa o desactiva una corp en el panel.
    """
    cached = cache.get("koru_corp_ids")
    if cached is not None:
        return cached
    ids = list(
        TrackedCorporation.objects
        .filter(is_active=True)
        .values_list("corporation_id", flat=True)
    )
    result = ids if ids else [0]  # 0 no dará resultados si no hay corps configuradas
    cache.set("koru_corp_ids", result, timeout=300)  # 5 minutos
    return result


def _corp_filter_sql(inicio, fin):
    """
    Devuelve el fragmento SQL y parámetros para filtrar
    personajes que estaban en las corps rastreadas durante el período.
    Se usa como JOIN en las queries principales.
    """
    sql = """
        JOIN alumni_charactercorporationhistory h
            ON h.character_id = up.main_character_id
            AND h.corporation_id IN ({placeholders})
            AND h.start_date < %s
        LEFT JOIN alumni_charactercorporationhistory next_h
            ON next_h.character_id = h.character_id
            AND next_h.record_id = (
                SELECT MIN(record_id)
                FROM alumni_charactercorporationhistory
                WHERE character_id = h.character_id
                AND record_id > h.record_id
            )
    """
    return sql, " AND (next_h.start_date IS NULL OR next_h.start_date >= %s)"


# ---------------------------------------------------------------------------
# Helpers ORM — leen de tablas pre-agregadas (sin JOINs pesados)
# ---------------------------------------------------------------------------

def _summary_top_mineros(corp_ids, period, limit=10):
    """Top mineros del período desde CharacterMonthlySummary."""
    qs = (CharacterMonthlySummary.objects
          .filter(period=period, corporation_id__in=corp_ids)
          .order_by("-mining_isk_reprocessed")[:limit])
    return [
        {
            "nombre":              r.main_character_name,
            "char_id":             r.main_character_id,
            "total_unidades":      int(r.mining_units),
            "total_m3":            float(r.mining_m3),
            "total_isk":           float(r.mining_isk),
            "total_isk_compressed":  float(r.mining_isk_compressed),
            "total_isk_reprocessed": float(r.mining_isk_reprocessed),
        }
        for r in qs
    ]


def _summary_top_bounties(corp_ids, period, limit=10):
    """Top bounties (bounty + ESS) del período desde CharacterMonthlySummary."""
    # Usamos combined_isk para evitar conflicto con @property total_isk del modelo
    qs = (CharacterMonthlySummary.objects
          .filter(period=period, corporation_id__in=corp_ids)
          .annotate(combined_isk=F("bounty_isk") + F("ess_isk"))
          .order_by("-combined_isk")[:limit])
    return [
        {
            "nombre":    r.main_character_name,
            "char_id":   r.main_character_id,
            "total_isk": float(r.combined_isk),
        }
        for r in qs
    ]


def _summary_ore_breakdown_corp(corp_ids, period):
    """Desglose de ore de la corp desde CharacterMonthlyOre."""
    rows = (CharacterMonthlyOre.objects
            .filter(period=period, corporation_id__in=corp_ids)
            .values("type_name", "type_id")
            .annotate(
                unidades=Sum("quantity"),
                m3_total=Sum("m3"),
                isk_estimado=Sum("isk"),
                isk_comp=Sum("isk_compressed"),
                isk_repr=Sum("isk_reprocessed"),
            )
            .order_by("-m3_total"))
    return [
        {
            "ore":          r["type_name"],
            "type_id":      r["type_id"],
            "unidades":     r["unidades"],
            "m3_total":     float(r["m3_total"] or 0),
            "isk_estimado": float(r["isk_estimado"] or 0),
            "isk_comp":     float(r["isk_comp"] or 0),
            "isk_repr":     float(r["isk_repr"] or 0),
        }
        for r in rows
    ]


def _summary_tendencias_mineria(corp_ids, n_months=6):
    """Tendencias de minería+bounties de los últimos N meses desde CharacterMonthlySummary."""
    hoy = datetime.now()
    periods = []
    for i in range(n_months):
        mes  = hoy.month - i
        anio = hoy.year
        if mes <= 0:
            mes  += 12
            anio -= 1
        periods.append(f"{anio}-{mes:02d}")

    rows = (CharacterMonthlySummary.objects
            .filter(period__in=periods, corporation_id__in=corp_ids)
            .values("period")
            .annotate(
                unidades=Sum("mining_units"),
                isk_mineria=Sum("mining_isk"),
                isk_bounties=Sum(F("bounty_isk") + F("ess_isk")),
            )
            .order_by("period"))
    return [dict(r) for r in rows]




def _summary_top_pvp(corp_ids, period, limit=10):
    """Top PvP del período por ISK destruido, desde CharacterMonthlyPvp."""
    qs = (CharacterMonthlyPvp.objects
          .filter(period=period, corporation_id__in=corp_ids)
          .order_by("-isk_destroyed")[:limit])
    return [
        {
            "nombre":         r.main_character_name,
            "char_id":        r.main_character_id,
            "ships_destroyed": r.ships_destroyed,
            "ships_lost":      r.ships_lost,
            "isk_destroyed":  float(r.isk_destroyed),
            "isk_lost":       float(r.isk_lost),
            "isk_efficiency": round(r.isk_efficiency, 1),
        }
        for r in qs
    ]


def _summary_pvp_tendencias(corp_ids, n_months=6):
    """Tendencia PvP mensual de los últimos N meses."""
    from datetime import datetime as dt
    hoy = dt.now()
    periods = []
    for i in range(n_months):
        mes  = hoy.month - i
        anio = hoy.year
        if mes <= 0:
            mes  += 12
            anio -= 1
        periods.append(f"{anio}-{mes:02d}")

    rows = (CharacterMonthlyPvp.objects
            .filter(period__in=periods, corporation_id__in=corp_ids)
            .values("period")
            .annotate(
                total_ships_destroyed=Sum("ships_destroyed"),
                total_ships_lost=Sum("ships_lost"),
                total_isk_destroyed=Sum("isk_destroyed"),
                total_isk_lost=Sum("isk_lost"),
                total_participations=Sum("participations"),
                total_solo_kills=Sum("solo_kills"),
                total_final_blows=Sum("final_blows"),
            )
            .order_by("period"))
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# SQL base — el filtro de corp se inyecta dinámicamente
# (se mantienen para corp_dashboard y vistas que no tienen tablas resumen)
# ---------------------------------------------------------------------------

def _build_top_mineros(corp_ids, inicio, fin):
    placeholders = ",".join(["%s"] * len(corp_ids))
    sql = f"""
        SELECT main_ec.character_name AS nombre, main_ec.character_id AS char_id,
               SUM(ml.quantity) AS total_unidades,
               ROUND(SUM(ml.quantity * it.volume), 2) AS total_m3,
               ROUND(SUM(ml.quantity * COALESCE(emp.average_price, 0)), 2) AS total_isk
        FROM corptools_characterminingledger ml
        JOIN corptools_characteraudit          cau     ON cau.id          = ml.character_id
        JOIN eveonline_evecharacter            ec      ON ec.id           = cau.character_id
        JOIN authentication_characterownership co      ON co.character_id = ec.id
        JOIN authentication_userprofile        up      ON up.user_id      = co.user_id
        JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id
        JOIN eve_sde_itemtype it ON it.id = ml.type_name_id
        LEFT JOIN eveuniverse_evemarketprice emp ON emp.eve_type_id = ml.type_name_id
        WHERE ml.date >= %s AND ml.date < %s
          AND ec.corporation_id IN ({placeholders})
        GROUP BY main_ec.id, main_ec.character_name, main_ec.character_id
        ORDER BY total_isk DESC LIMIT 10
    """
    return sql, [inicio, fin] + corp_ids


def _build_top_bounties(corp_ids, inicio, fin):
    placeholders = ",".join(["%s"] * len(corp_ids))
    sql = f"""
        SELECT main_ec.character_name AS nombre, main_ec.character_id AS char_id,
               SUM(wj.amount) AS total_isk
        FROM corptools_characterwalletjournalentry wj
        JOIN corptools_characteraudit          cau     ON cau.id          = wj.character_id
        JOIN eveonline_evecharacter            ec      ON ec.id           = cau.character_id
        JOIN authentication_characterownership co      ON co.character_id = ec.id
        JOIN authentication_userprofile        up      ON up.user_id      = co.user_id
        JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id
        WHERE wj.ref_type IN ('bounty_prizes', 'ess_escrow_transfer')
          AND wj.amount > 0
          AND wj.date >= %s AND wj.date < %s
          AND ec.corporation_id IN ({placeholders})
        GROUP BY main_ec.id, main_ec.character_name, main_ec.character_id
        ORDER BY total_isk DESC LIMIT 10
    """
    return sql, [inicio, fin] + corp_ids


def _build_ore_breakdown_corp(corp_ids, inicio, fin):
    placeholders = ",".join(["%s"] * len(corp_ids))
    sql = f"""
        SELECT it.name AS ore,
               SUM(ml.quantity) AS unidades,
               ROUND(SUM(ml.quantity * it.volume), 2) AS m3_total,
               ROUND(SUM(ml.quantity * COALESCE(emp.average_price, 0)), 2) AS isk_estimado
        FROM corptools_characterminingledger ml
        JOIN corptools_characteraudit          cau     ON cau.id          = ml.character_id
        JOIN eveonline_evecharacter            ec      ON ec.id           = cau.character_id
        JOIN authentication_characterownership co      ON co.character_id = ec.id
        JOIN authentication_userprofile        up      ON up.user_id      = co.user_id
        JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id
        JOIN eve_sde_itemtype it ON it.id = ml.type_name_id
        LEFT JOIN eveuniverse_evemarketprice emp ON emp.eve_type_id = ml.type_name_id
        WHERE ml.date >= %s AND ml.date < %s
          AND ec.corporation_id IN ({placeholders})
        GROUP BY it.id, it.name, it.volume
        ORDER BY m3_total DESC
    """
    return sql, [inicio, fin] + corp_ids


SQL_MINING_PERSONAL = """
    SELECT ec.character_name AS nombre, ec.character_id AS char_id,
           SUM(ml.quantity)                                                    AS total_unidades,
           ROUND(SUM(ml.quantity * COALESCE(it.volume, 0)), 2)                 AS total_m3,
           ROUND(SUM(ml.quantity * COALESCE(orp.price_raw, 0)), 2)             AS isk_raw,
           ROUND(SUM(ml.quantity * COALESCE(orp.price_compressed, 0)), 2)      AS isk_comp,
           ROUND(SUM(ml.quantity * COALESCE(orp.price_reprocessed, 0)), 2)     AS isk_repr
    FROM corptools_characterminingledger ml
    JOIN corptools_characteraudit          cau ON cau.id          = ml.character_id
    JOIN eveonline_evecharacter            ec  ON ec.id           = cau.character_id
    JOIN authentication_characterownership co  ON co.character_id = ec.id
    JOIN authentication_userprofile        up  ON up.user_id      = co.user_id
    JOIN eve_sde_itemtype                  it  ON it.id           = ml.type_name_id
    LEFT JOIN koru_stats_oremarketprice    orp ON orp.type_id     = ml.type_name_id
    WHERE up.main_character_id = %s AND ml.date >= %s AND ml.date < %s
    GROUP BY ec.id, ec.character_name, ec.character_id
    ORDER BY isk_repr DESC
"""

SQL_BOUNTIES_DIARIOS = """
    SELECT DATE(wj.date) AS dia, SUM(wj.amount) AS total_isk
    FROM corptools_characterwalletjournalentry wj
    JOIN corptools_characteraudit          cau ON cau.id          = wj.character_id
    JOIN eveonline_evecharacter            ec  ON ec.id           = cau.character_id
    JOIN authentication_characterownership co  ON co.character_id = ec.id
    JOIN authentication_userprofile        up  ON up.user_id      = co.user_id
    WHERE up.main_character_id = %s
      AND wj.ref_type IN ('bounty_prizes', 'ess_escrow_transfer')
      AND wj.amount > 0
      AND wj.date >= %s AND wj.date < %s
    GROUP BY DATE(wj.date) ORDER BY dia ASC
"""

SQL_ORE_BREAKDOWN_PERSONAL = """
    SELECT it.name AS ore,
           SUM(ml.quantity) AS unidades,
           ROUND(SUM(ml.quantity * it.volume), 2) AS m3_total,
           ROUND(SUM(ml.quantity * COALESCE(emp.average_price, 0)), 2) AS isk_estimado
    FROM corptools_characterminingledger ml
    JOIN corptools_characteraudit          cau ON cau.id          = ml.character_id
    JOIN eveonline_evecharacter            ec  ON ec.id           = cau.character_id
    JOIN authentication_characterownership co  ON co.character_id = ec.id
    JOIN authentication_userprofile        up  ON up.user_id      = co.user_id
    JOIN eve_sde_itemtype                  it  ON it.id           = ml.type_name_id
    LEFT JOIN eveuniverse_evemarketprice   emp ON emp.eve_type_id = ml.type_name_id
    WHERE up.main_character_id = %s AND ml.date >= %s AND ml.date < %s
    GROUP BY it.id, it.name, it.volume
    ORDER BY m3_total DESC
"""


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _add_comparativa(actual, anterior, campo):
    """Añade delta % vs mes anterior a cada fila del top."""
    ant_map = {r["nombre"]: float(r[campo] or 0) for r in anterior}
    result = []
    for r in actual:
        r = dict(r)
        val_act = float(r.get(campo) or 0)
        val_ant = ant_map.get(r["nombre"], 0)
        if val_ant > 0:
            delta = ((val_act - val_ant) / val_ant) * 100
            r["delta"]      = round(delta, 1)
            r["delta_pct"]  = round(abs(delta), 1)
            r["delta_dir"]  = "up" if delta >= 0 else "down"
        else:
            r["delta"]      = None
            r["delta_pct"]  = None
            r["delta_dir"]  = "new"
        result.append(r)
    return result


def _mes_anterior(anio, mes):
    """Devuelve inicio y fin del mes anterior."""
    if mes == 1:
        return _rango_mes(anio - 1, 12)
    return _rango_mes(anio, mes - 1)


@permission_required("koru_stats.basic_access")
def dashboard(request):
    mes, anio, inicio, fin, periodo_sel = _parse_periodo(request)
    periodos_datos = _get_periodos_con_datos("general")
    selector_ctx   = _build_selector_context(periodos_datos, periodo_sel, anio)
    corp_ids   = _get_corp_ids()
    corp_names = list(TrackedCorporation.objects.filter(is_active=True).values_list("corporation_name", flat=True))

    # Mes anterior para comparativa
    if mes == 1:
        period_ant = f"{anio - 1}-12"
    else:
        period_ant = f"{anio}-{mes - 1:02d}"

    top_mineros, top_bounties, ore_breakdown = [], [], []
    top_mineros_ant, top_bounties_ant = [], []
    error_mineros = error_bounties = error_ore = False

    # ── Top mineros y bounties — ORM sobre CharacterMonthlySummary (sin JOINs) ──
    try:
        top_mineros     = _summary_top_mineros(corp_ids, periodo_sel)
        top_mineros_ant = _summary_top_mineros(corp_ids, period_ant)
    except Exception as e:
        logger.error("koru_stats top_mineros: %s", e)
        error_mineros = True

    try:
        top_bounties     = _summary_top_bounties(corp_ids, periodo_sel)
        top_bounties_ant = _summary_top_bounties(corp_ids, period_ant)
    except Exception as e:
        logger.error("koru_stats top_bounties: %s", e)
        error_bounties = True

    # ── Ore breakdown — ORM sobre CharacterMonthlyOre ──
    try:
        ore_breakdown = _summary_ore_breakdown_corp(corp_ids, periodo_sel)
    except Exception as e:
        logger.error("koru_stats ore_breakdown: %s", e)
        error_ore = True

    total_m3      = sum(float(r["m3_total"] or 0) for r in ore_breakdown)
    total_isk_ore = sum(float(r["isk_estimado"] or 0) for r in ore_breakdown)
    top_ore_chart = sorted(ore_breakdown, key=lambda r: float(r["isk_estimado"] or 0), reverse=True)[:8]

    # ── Tendencias — minería desde CharacterMonthlySummary, bounties desde corp wallet ──
    tendencias_bounties = []
    try:
        tendencias_mineria = _summary_tendencias_mineria(corp_ids)
    except Exception as e:
        logger.error("koru_stats tendencias_mineria: %s", e)
        tendencias_mineria = []

    # ── Top PvP — desde CharacterMonthlyPvp ──
    top_pvp = []
    error_pvp = False
    pvp_tend  = []
    try:
        top_pvp  = _summary_top_pvp(corp_ids, periodo_sel)
        pvp_tend = _summary_pvp_tendencias(corp_ids)
    except Exception as e:
        logger.error("koru_stats top_pvp: %s", e)
        error_pvp = True

    # ── KPI agregados corp ──
    total_isk_mining   = sum(float(r.get("total_isk", 0) or 0) for r in top_mineros)
    total_isk_bounties = sum(float(r.get("total_isk", 0) or 0) for r in top_bounties)
    pvp_agg = CharacterMonthlyPvp.objects.filter(
        period=periodo_sel, corporation_id__in=corp_ids
    ).aggregate(
        k=Sum("ships_destroyed"), d=Sum("ships_lost"),
        id=Sum("isk_destroyed"),  il=Sum("isk_lost"),
    )
    total_pvp_kills = int(pvp_agg["k"] or 0)
    total_pvp_deaths = int(pvp_agg["d"] or 0)
    _pvp_id = float(pvp_agg["id"] or 0)
    _pvp_il = float(pvp_agg["il"] or 0)
    corp_pvp_eff = round(_pvp_id / (_pvp_id + _pvp_il) * 100, 1) if (_pvp_id + _pvp_il) else 0.0

    # ── Tendencias combinadas ──
    periodos_tend = [r["period"] for r in tendencias_mineria]
    min_by_period  = {r["period"]: float(r["isk_mineria"]  or 0) for r in tendencias_mineria}
    bou_by_period  = {r["period"]: float(r["isk_bounties"] or 0) for r in tendencias_mineria}
    uni_by_period  = {r["period"]: int(r["unidades"]       or 0) for r in tendencias_mineria}
    pvp_by_period  = {r["period"]: float(r["total_isk_destroyed"] or 0) for r in pvp_tend}
    # unión de todos los períodos
    all_periods = sorted(set(periodos_tend) | set(pvp_by_period.keys()))

    context = {
        "mes":               date(anio, mes, 1).strftime("%B %Y"),
        **selector_ctx,
        "corp_names":        corp_names,
        "top_mineros":       _add_comparativa(top_mineros, top_mineros_ant, "total_isk"),
        "top_bounties":      _add_comparativa(top_bounties, top_bounties_ant, "total_isk"),
        "ore_breakdown":     ore_breakdown,
        "total_m3":          total_m3,
        "total_isk_ore":     total_isk_ore,
        "error_mineros":     error_mineros,
        "error_bounties":    error_bounties,
        "error_ore":         error_ore,
        # KPI cards
        "total_isk_mining":    total_isk_mining,
        "total_isk_bounties":  total_isk_bounties,
        "total_pvp_kills":     total_pvp_kills,
        "total_pvp_deaths":    total_pvp_deaths,
        "corp_pvp_eff":        corp_pvp_eff,
        # Charts individuales
        "chart_mineros":  _to_json({
            "labels":    [r["nombre"] for r in top_mineros],
            "unidades":  [int(r["total_unidades"]) for r in top_mineros],
            "m3":        [float(r["total_m3"] or 0) for r in top_mineros],
            "isk":       [float(r["total_isk"] or 0) for r in top_mineros],
            "isk_comp":  [float(r.get("total_isk_compressed", 0) or 0) for r in top_mineros],
            "isk_repr":  [float(r.get("total_isk_reprocessed", 0) or 0) for r in top_mineros],
        }),
        "chart_bounties": _to_json({"labels": [r["nombre"] for r in top_bounties], "data": [float(Decimal(str(r["total_isk"]))) for r in top_bounties]}),
        "chart_ore":      _to_json({"labels": [r["ore"] for r in top_ore_chart], "data": [float(r["isk_estimado"] or 0) for r in top_ore_chart]}),
        # Chart combinado tendencia (todos los períodos)
        "chart_tendencia_combinada": _to_json({
            "labels":   all_periods,
            "mineria":  [min_by_period.get(p, 0) / 1e9  for p in all_periods],
            "bounties": [bou_by_period.get(p, 0) / 1e9  for p in all_periods],
            "pvp":      [pvp_by_period.get(p, 0) / 1e9  for p in all_periods],
        }),
        "top_pvp":     top_pvp,
        "error_pvp":   error_pvp,
        "chart_pvp": _to_json({
            "labels":          [r["nombre"] for r in top_pvp],
            "isk_destroyed":   [r["isk_destroyed"] for r in top_pvp],
            "ships_destroyed": [r["ships_destroyed"] for r in top_pvp],
        }),
    }
    return render(request, "koru_stats/dashboard.html", context)


@permission_required("koru_stats.basic_access")
def mi_dashboard(request):
    try:
        main = request.user.profile.main_character
    except Exception:
        main = None

    if not main:
        return render(request, "koru_stats/mi_dashboard.html", {"sin_main": True})

    mes, anio, inicio, fin, periodo_sel = _parse_periodo(request)
    periodos_datos = _get_periodos_con_datos("general")
    selector_ctx   = _build_selector_context(periodos_datos, periodo_sel, anio)

    mining_personal, bounties_diarios, ore_breakdown = [], [], []
    error_mining = error_bounties = error_ore = False

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_MINING_PERSONAL, [main.id, inicio, fin])
            mining_personal = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats mining personal: %s", e)
        error_mining = True

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_BOUNTIES_DIARIOS, [main.id, inicio, fin])
            bounties_diarios = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats bounties diarios: %s", e)
        error_bounties = True

    try:
        # ORM sobre CharacterMonthlyOre — sin JOINs
        # main.character_id = EVE Online ID (el que guardamos en tasks.py)
        # main.id = Django PK (diferente)
        ore_rows = (CharacterMonthlyOre.objects
                    .filter(main_character_id=main.character_id, period=periodo_sel)
                    .order_by("-m3"))
        ore_breakdown = [
            {
                "ore":          r.type_name,
                "unidades":     int(r.quantity),
                "m3_total":     float(r.m3),
                "isk_estimado": float(r.isk),
                "isk_comp":     float(r.isk_compressed),
                "isk_repr":     float(r.isk_reprocessed),
            }
            for r in ore_rows
        ]
    except Exception as e:
        logger.error("koru_stats ore_breakdown personal: %s", e)
        error_ore = True

    # ── PvP personal — desde CharacterMonthlyPvp ──
    pvp_personal = None
    pvp_tendencia = []
    error_pvp_personal = False
    try:
        pvp_personal = CharacterMonthlyPvp.objects.filter(
            main_character_id=main.character_id, period=periodo_sel
        ).first()
        # Historial últimos 6 meses para gráfica
        from datetime import datetime as dt
        hoy = dt.now()
        pvp_periods = []
        for i in range(6):
            ms = hoy.month - i; ay = hoy.year
            if ms <= 0: ms += 12; ay -= 1
            pvp_periods.append(f"{ay}-{ms:02d}")
        pvp_tendencia = list(
            CharacterMonthlyPvp.objects
            .filter(main_character_id=main.character_id, period__in=pvp_periods)
            .order_by("period")
            .values("period", "ships_destroyed", "ships_lost", "isk_destroyed", "isk_lost")
        )
    except Exception as e:
        logger.error("koru_stats pvp_personal: %s", e)
        error_pvp_personal = True

    pvp_kills  = []
    pvp_losses = []
    try:
        pvp_kills = list(
            CharacterKillRecord.objects
            .filter(main_character_id=main.character_id, period=periodo_sel, is_loss=False)
            .order_by("-kill_date", "-killmail_id")
            .values("killmail_id", "ship_type_id", "ship_name", "value_isk", "kill_date", "final_blow", "solo")
        )
        pvp_losses = list(
            CharacterKillRecord.objects
            .filter(main_character_id=main.character_id, period=periodo_sel, is_loss=True)
            .order_by("-kill_date", "-killmail_id")
            .values("killmail_id", "ship_type_id", "ship_name", "value_isk", "kill_date")
        )
    except Exception as e:
        logger.error("koru_stats kill_records: %s", e)

    mining_sistemas, bounties_sistemas = [], []
    error_mining_sis = error_bounties_sis = False
    bounties_desglose = {"bounties_directos": 0, "ess_pagos": 0, "total": 0}

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_MINING_POR_SISTEMA, [main.id, inicio, fin])
            mining_sistemas = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats mining_sistemas: %s", e)
        error_mining_sis = True

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_BOUNTIES_POR_SISTEMA, [main.id, inicio, fin])
            bounties_sistemas = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats bounties_sistemas: %s", e)
        error_bounties_sis = True

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_BOUNTIES_DESGLOSE, [main.id, inicio, fin])
            row = cursor.fetchone()
            if row:
                bounties_desglose = {
                    "bounties_directos": float(row[0] or 0),
                    "ess_pagos":         float(row[1] or 0),
                    "total":             float(row[2] or 0),
                }
    except Exception as e:
        logger.error("koru_stats bounties_desglose: %s", e)


    # ── Wallet personal por categorías ─────────────────────────────────────
    ingresos_personal, gastos_personal = [], []
    balance_personal = 0
    total_ing_p = total_gas_p = 0
    ingresos_donut, gastos_donut = [], []
    error_wallet_personal = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_PERSONAL_WALLET_INCOME, [main.id, inicio, fin])
            rows_ing = _fetchall(cursor)
        ingresos_personal = _categorize_wallet(rows_ing, PERSONAL_INCOME_CATEGORIES)

        with connection.cursor() as cursor:
            cursor.execute(SQL_PERSONAL_WALLET_EXPENSE, [main.id, inicio, fin])
            rows_gas = _fetchall(cursor)
        gastos_personal = _categorize_wallet(rows_gas, PERSONAL_EXPENSE_CATEGORIES)

        total_ing_p = sum(r["total"] for r in ingresos_personal)
        total_gas_p = sum(r["total"] for r in gastos_personal)
        balance_personal = round(total_ing_p - total_gas_p, 2)

        ingresos_donut = [{"cat": r["categoria"], "total": r["total"]} for r in ingresos_personal]
        gastos_donut   = [{"cat": r["categoria"], "total": r["total"]} for r in gastos_personal]

        # Comparativa vs mes anterior
        inicio_ant, fin_ant = _mes_anterior(anio, mes)
        with connection.cursor() as cursor:
            cursor.execute(SQL_PERSONAL_WALLET_INCOME, [main.id, inicio_ant, fin_ant])
            rows_ing_ant = _fetchall(cursor)
        ing_ant = _categorize_wallet(rows_ing_ant, PERSONAL_INCOME_CATEGORIES)

        with connection.cursor() as cursor:
            cursor.execute(SQL_PERSONAL_WALLET_EXPENSE, [main.id, inicio_ant, fin_ant])
            rows_gas_ant = _fetchall(cursor)
        gas_ant = _categorize_wallet(rows_gas_ant, PERSONAL_EXPENSE_CATEGORIES)

        # Comparativa por categoría (usa "categoria" como clave, no "nombre")
        ing_ant_map = {r["categoria"]: r["total"] for r in ing_ant}
        for r in ingresos_personal:
            val_ant = ing_ant_map.get(r["categoria"], 0)
            if val_ant > 0:
                r["delta"] = round(((r["total"] - val_ant) / val_ant) * 100, 1)
            else:
                r["delta"] = None

        gas_ant_map = {r["categoria"]: r["total"] for r in gas_ant}
        for r in gastos_personal:
            val_ant = gas_ant_map.get(r["categoria"], 0)
            if val_ant > 0:
                r["delta"] = round(((r["total"] - val_ant) / val_ant) * 100, 1)
            else:
                r["delta"] = None

        total_ing_p_ant  = sum(r["total"] for r in ing_ant)
        total_gas_p_ant  = sum(r["total"] for r in gas_ant)
        balance_p_ant    = round(total_ing_p_ant - total_gas_p_ant, 2)

    except Exception as e:
        logger.error("koru_stats wallet personal: %s", e)
        error_wallet_personal = True
        total_ing_p_ant = total_gas_p_ant = balance_p_ant = 0
    # ── Mes anterior: minería + bounties desde CharacterMonthlySummary ──
    anio_ant, mes_ant = (anio, mes - 1) if mes > 1 else (anio - 1, 12)
    periodo_ant = f"{anio_ant}-{mes_ant:02d}"
    try:
        summary_ant = CharacterMonthlySummary.objects.filter(
            main_character_id=main.character_id, period=periodo_ant
        ).first()
        mining_isk_ore_repr_ant = float(summary_ant.mining_isk_reprocessed) if summary_ant else 0
        mining_isk_ore_comp_ant = float(summary_ant.mining_isk_compressed)  if summary_ant else 0
        mining_isk_ore_raw_ant  = float(summary_ant.mining_isk)             if summary_ant else 0
        total_bounties_ant      = float((summary_ant.bounty_isk or 0) + (summary_ant.ess_isk or 0)) if summary_ant else 0
    except Exception as e:
        logger.error("koru_stats summary_ant personal: %s", e)
        mining_isk_ore_repr_ant = mining_isk_ore_comp_ant = mining_isk_ore_raw_ant = total_bounties_ant = 0

    # ensure wallet-ant totals exist even if wallet block errored
    if "total_ing_p_ant" not in dir():
        total_ing_p_ant = total_gas_p_ant = balance_p_ant = 0

    total_minado   = sum(int(r["total_unidades"]) for r in mining_personal)
    total_bounties = sum(float(Decimal(str(r["total_isk"]))) for r in bounties_diarios)
    total_m3       = sum(float(r["m3_total"] or 0) for r in ore_breakdown)
    total_isk_ore      = sum(float(r["isk_estimado"] or 0) for r in ore_breakdown)
    total_isk_ore_comp = sum(float(r["isk_comp"]     or 0) for r in ore_breakdown)
    total_isk_ore_repr = sum(float(r["isk_repr"]     or 0) for r in ore_breakdown)
    top_ore_chart  = sorted(ore_breakdown, key=lambda r: float(r["isk_estimado"] or 0), reverse=True)[:8]

    context = {
        "main":              main,
        "mes":               date(anio, mes, 1).strftime("%B %Y"),
        **selector_ctx,
        "mining_personal":   mining_personal,
        "bounties_diarios":  bounties_diarios,
        "ore_breakdown":     ore_breakdown,
        "total_minado":      total_minado,
        "total_bounties":    total_bounties,
        "total_m3":          total_m3,
        "total_isk_ore":      total_isk_ore,
        "total_isk_ore_comp": total_isk_ore_comp,
        "total_isk_ore_repr": total_isk_ore_repr,
        "error_mining":       error_mining,
        "error_bounties":    error_bounties,
        "error_ore":         error_ore,
        "chart_mining_personal": _to_json({
            "labels":   [r["nombre"]          for r in mining_personal],
            "unidades": [int(r["total_unidades"] or 0) for r in mining_personal],
            "m3":       [float(r["total_m3"]    or 0) for r in mining_personal],
            "isk":      [float(r["isk_raw"]     or 0) for r in mining_personal],
            "isk_comp": [float(r["isk_comp"]    or 0) for r in mining_personal],
            "isk_repr": [float(r["isk_repr"]    or 0) for r in mining_personal],
        }),
        "chart_bounties_dia":    _to_json({"labels": [str(r["dia"]) for r in bounties_diarios], "data": [float(Decimal(str(r["total_isk"]))) for r in bounties_diarios]}),
        "chart_ore":             _to_json({"labels": [r["ore"] for r in top_ore_chart], "data": [float(r["isk_estimado"] or 0) for r in top_ore_chart]}),
        "mining_sistemas":       mining_sistemas,
        "bounties_sistemas":     bounties_sistemas,
        "error_mining_sis":      error_mining_sis,
        "error_bounties_sis":    error_bounties_sis,
        "chart_mining_sis":      _to_json({"labels": [r["sistema"] for r in mining_sistemas], "data": [float(r["isk_estimado"] or 0) for r in mining_sistemas]}),
        "chart_bounties_sis":    _to_json({"labels": [r["sistema"] for r in bounties_sistemas], "data": [float(Decimal(str(r["total_isk"]))) for r in bounties_sistemas]}),
        "bounties_desglose":     bounties_desglose,
        "ingresos_personal":     ingresos_personal,
        "gastos_personal":       gastos_personal,
        "total_ingresos_p":      total_ing_p,
        "total_gastos_p":        total_gas_p,
        "balance_personal":      balance_personal,
        "balance_personal_json": json.dumps(float(balance_personal)),
        "total_ing_p_ant":        total_ing_p_ant,
        "total_gas_p_ant":        total_gas_p_ant,
        "balance_p_ant":          balance_p_ant,
        "mining_isk_ore_repr_ant": mining_isk_ore_repr_ant,
        "mining_isk_ore_comp_ant": mining_isk_ore_comp_ant,
        "mining_isk_ore_raw_ant":  mining_isk_ore_raw_ant,
        "total_bounties_ant":      total_bounties_ant,
        "error_wallet_personal": error_wallet_personal,
        "chart_ingresos_p":      _to_json({"labels": [r["cat"] for r in ingresos_donut], "data": [r["total"] for r in ingresos_donut]}),
        "chart_gastos_p":        _to_json({"labels": [r["cat"] for r in gastos_donut],   "data": [r["total"] for r in gastos_donut]}),
        "pvp_personal":        pvp_personal,
        "pvp_tendencia":       pvp_tendencia,
        "pvp_kills":           pvp_kills,
        "pvp_losses":          pvp_losses,
        "error_pvp_personal":  error_pvp_personal,
        "chart_pvp_personal":  _to_json({
            "labels":          [r["period"] for r in pvp_tendencia],
            "isk_destroyed":   [float(r["isk_destroyed"] or 0) for r in pvp_tendencia],
            "isk_lost":        [float(r["isk_lost"] or 0) for r in pvp_tendencia],
            "ships_destroyed": [int(r["ships_destroyed"] or 0) for r in pvp_tendencia],
            "ships_lost":      [int(r["ships_lost"] or 0) for r in pvp_tendencia],
        }),
    }
    return render(request, "koru_stats/mi_dashboard.html", context)


# ---------------------------------------------------------------------------
# Queries adicionales para mi_dashboard ampliado
# ---------------------------------------------------------------------------

SQL_MINING_POR_SISTEMA = """
    SELECT ms.name AS sistema,
           ms.security_status AS sec,
           SUM(ml.quantity)                                                    AS unidades,
           ROUND(SUM(ml.quantity * COALESCE(it.volume, 0)), 2)                 AS m3_total,
           ROUND(SUM(ml.quantity * COALESCE(orp.price_raw, 0)), 2)             AS isk_estimado,
           ROUND(SUM(ml.quantity * COALESCE(orp.price_compressed, 0)), 2)      AS isk_comp,
           ROUND(SUM(ml.quantity * COALESCE(orp.price_reprocessed, 0)), 2)     AS isk_repr
    FROM corptools_characterminingledger ml
    JOIN corptools_characteraudit          cau ON cau.id          = ml.character_id
    JOIN eveonline_evecharacter            ec  ON ec.id           = cau.character_id
    JOIN authentication_characterownership co  ON co.character_id = ec.id
    JOIN authentication_userprofile        up  ON up.user_id      = co.user_id
    JOIN corptools_mapsystem               ms  ON ms.system_id    = ml.system_id
    JOIN eve_sde_itemtype                  it  ON it.id           = ml.type_name_id
    LEFT JOIN koru_stats_oremarketprice    orp ON orp.type_id     = ml.type_name_id
    WHERE up.main_character_id = %s
      AND ml.date >= %s AND ml.date < %s
    GROUP BY ms.system_id, ms.name, ms.security_status
    ORDER BY isk_repr DESC
"""

SQL_BOUNTIES_POR_SISTEMA = """
    SELECT ms.name AS sistema,
           ms.security_status AS sec,
           SUM(wj.amount) AS total_isk,
           COUNT(*) AS entradas
    FROM corptools_characterwalletjournalentry wj
    JOIN corptools_characteraudit          cau ON cau.id          = wj.character_id
    JOIN eveonline_evecharacter            ec  ON ec.id           = cau.character_id
    JOIN authentication_characterownership co  ON co.character_id = ec.id
    JOIN authentication_userprofile        up  ON up.user_id      = co.user_id
    JOIN corptools_mapsystem               ms  ON ms.system_id    = wj.context_id
    WHERE up.main_character_id = %s
      AND wj.ref_type IN ('bounty_prizes', 'ess_escrow_transfer')
      AND wj.amount > 0
      AND wj.date >= %s AND wj.date < %s
    GROUP BY ms.system_id, ms.name, ms.security_status
    ORDER BY total_isk DESC
"""

SQL_BOUNTIES_DESGLOSE = """
    SELECT
        SUM(CASE WHEN wj.ref_type = 'bounty_prizes'      THEN wj.amount ELSE 0 END) AS bounties_directos,
        SUM(CASE WHEN wj.ref_type = 'ess_escrow_transfer' THEN wj.amount ELSE 0 END) AS ess_pagos,
        SUM(wj.amount) AS total
    FROM corptools_characterwalletjournalentry wj
    JOIN corptools_characteraudit          cau ON cau.id          = wj.character_id
    JOIN eveonline_evecharacter            ec  ON ec.id           = cau.character_id
    JOIN authentication_characterownership co  ON co.character_id = ec.id
    JOIN authentication_userprofile        up  ON up.user_id      = co.user_id
    WHERE up.main_character_id = %s
      AND wj.ref_type IN ('bounty_prizes', 'ess_escrow_transfer')
      AND wj.amount > 0
      AND wj.date >= %s AND wj.date < %s
"""


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Mi Dashboard — wallet personal por categoría
# ---------------------------------------------------------------------------
SQL_PERSONAL_WALLET = """
    SELECT wj.ref_type,
           ROUND(SUM(wj.amount), 2) AS total
    FROM corptools_characterwalletjournalentry wj
    JOIN corptools_characteraudit          cau ON cau.id          = wj.character_id
    JOIN eveonline_evecharacter            ec  ON ec.id           = cau.character_id
    JOIN authentication_characterownership co  ON co.character_id = ec.id
    JOIN authentication_userprofile        up  ON up.user_id      = co.user_id
    WHERE up.main_character_id = %s
      AND wj.date >= %s AND wj.date < %s
    GROUP BY wj.ref_type
    ORDER BY ABS(SUM(wj.amount)) DESC
"""

SQL_PERSONAL_WALLET_INCOME = """
    SELECT wj.ref_type,
           ROUND(SUM(wj.amount), 2) AS total
    FROM corptools_characterwalletjournalentry wj
    JOIN corptools_characteraudit          cau ON cau.id          = wj.character_id
    JOIN eveonline_evecharacter            ec  ON ec.id           = cau.character_id
    JOIN authentication_characterownership co  ON co.character_id = ec.id
    JOIN authentication_userprofile        up  ON up.user_id      = co.user_id
    WHERE up.main_character_id = %s
      AND wj.date >= %s AND wj.date < %s
      AND wj.amount > 0
    GROUP BY wj.ref_type
    ORDER BY SUM(wj.amount) DESC
"""

SQL_PERSONAL_WALLET_EXPENSE = """
    SELECT wj.ref_type,
           ROUND(SUM(wj.amount), 2) AS total
    FROM corptools_characterwalletjournalentry wj
    JOIN corptools_characteraudit          cau ON cau.id          = wj.character_id
    JOIN eveonline_evecharacter            ec  ON ec.id           = cau.character_id
    JOIN authentication_characterownership co  ON co.character_id = ec.id
    JOIN authentication_userprofile        up  ON up.user_id      = co.user_id
    WHERE up.main_character_id = %s
      AND wj.date >= %s AND wj.date < %s
      AND wj.amount < 0
    GROUP BY wj.ref_type
    ORDER BY SUM(wj.amount) ASC
"""

# Corp Dashboard — queries
# ---------------------------------------------------------------------------

SQL_CORP_RESUMEN = """
    SELECT ref_type,
           SUM(amount)  AS total,
           COUNT(*)     AS entradas
    FROM corptools_corporationwalletjournalentry
    WHERE amount > 0
      AND date >= %s AND date < %s
    GROUP BY ref_type
    ORDER BY total DESC
    LIMIT 15
"""

SQL_CORP_TOP_CONTRIBUIDORES = """
    SELECT main_ec.character_name AS nombre,
           main_ec.character_id   AS char_id,
           SUM(wj.amount)         AS total_tax,
           SUM(CASE WHEN wj.ref_type = 'bounty_prizes'       THEN wj.amount ELSE 0 END) AS bounties,
           SUM(CASE WHEN wj.ref_type = 'ess_escrow_transfer' THEN wj.amount ELSE 0 END) AS ess,
           SUM(CASE WHEN wj.ref_type = 'player_donation'     THEN wj.amount ELSE 0 END) AS donaciones
    FROM corptools_corporationwalletjournalentry wj
    JOIN eveonline_evecharacter            ec      ON ec.character_id  = wj.second_party_id
    JOIN authentication_characterownership co      ON co.character_id  = ec.id
    JOIN authentication_userprofile        up      ON up.user_id       = co.user_id
    JOIN eveonline_evecharacter            main_ec ON main_ec.id       = up.main_character_id
    WHERE wj.ref_type IN ('bounty_prizes', 'ess_escrow_transfer', 'player_donation')
      AND wj.amount > 0
      AND wj.date >= %s AND wj.date < %s
      AND wj.second_party_id != 98176563
    GROUP BY main_ec.id, main_ec.character_name, main_ec.character_id
    ORDER BY total_tax DESC
    LIMIT 10
"""

SQL_CORP_INGRESOS_DIARIOS = """
    SELECT DATE(date) AS dia,
           SUM(CASE WHEN ref_type IN ('bounty_prizes','ess_escrow_transfer') THEN amount ELSE 0 END) AS bounties,
           SUM(CASE WHEN ref_type = 'industry_job_tax'       THEN amount ELSE 0 END) AS industry,
           SUM(CASE WHEN ref_type = 'corporate_reward_payout' THEN amount ELSE 0 END) AS ded,
           SUM(CASE WHEN ref_type = 'player_donation'         THEN amount ELSE 0 END) AS donaciones,
           SUM(amount) AS total
    FROM corptools_corporationwalletjournalentry
    WHERE amount > 0
      AND date >= %s AND date < %s
    GROUP BY DATE(date)
    ORDER BY dia ASC
"""

SQL_CORP_TOP_SISTEMAS = """
    SELECT ms.name AS sistema,
           ms.security_status AS sec,
           SUM(wj.amount) AS total_isk,
           COUNT(DISTINCT wj.second_party_id) AS pilotos
    FROM corptools_corporationwalletjournalentry wj
    JOIN corptools_mapsystem ms ON ms.system_id = wj.context_id
    WHERE wj.ref_type IN ('bounty_prizes', 'ess_escrow_transfer')
      AND wj.amount > 0
      AND wj.date >= %s AND wj.date < %s
    GROUP BY ms.system_id, ms.name, ms.security_status
    ORDER BY total_isk DESC
    LIMIT 10
"""

# Etiquetas legibles para ref_type
REF_TYPE_LABELS = {
    'bounty_prizes':              'Bounties',
    'ess_escrow_transfer':        'ESS Pagos',
    'industry_job_tax':           'Tax Industria',
    'corporate_reward_payout':    'DED / Incursiones',
    'daily_goal_payouts':         'Daily Goals',
    'player_donation':            'Movimientos de Wallet',
    'market_transaction':         'Mercado',
    'reprocessing_tax':           'Tax Reprocesado',
    'office_rental_fee':          'Alquiler Oficinas',
    'contract_price_payment_corp':'Contratos Corp',
    'jump_clone_activation_fee':  'Jump Clones',
    'project_discovery_reward':   'Project Discovery',
    'agent_mission_reward':       'Misiones Agente',
}


@permission_required("koru_stats.corp_finance_access")
def corp_dashboard(request):
    mes, anio, inicio, fin, periodo_sel = _parse_periodo(request)
    periodos_datos = _get_periodos_con_datos("corp")
    corp_names = list(TrackedCorporation.objects.filter(is_active=True).values_list("corporation_name", flat=True))
    selector_ctx   = _build_selector_context(periodos_datos, periodo_sel, anio)
    inicio_ant, fin_ant = _mes_anterior(anio, mes)
    resumen, contribuidores, ingresos_diarios, top_sistemas = [], [], [], []
    corp_ingresos_cat, corp_gastos_cat = [], []
    error_resumen = error_contrib = error_diarios = error_sistemas = False

    try:
        sql, params = _build_corp_wallet_by_category(inicio, fin, only_income=True)
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = _fetchall(cursor)
            corp_ingresos_cat = _categorize_wallet(rows, CORP_INCOME_CATEGORIES, True)
    except Exception as e:
        logger.error("koru_stats corp_ingresos_cat: %s", e)

    try:
        sql, params = _build_corp_wallet_by_category(inicio, fin, only_income=False)
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = _fetchall(cursor)
            corp_gastos_cat = _categorize_wallet(rows, CORP_EXPENSE_CATEGORIES, False)
    except Exception as e:
        logger.error("koru_stats corp_gastos_cat: %s", e)

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_CORP_RESUMEN, [inicio, fin])
            rows = _fetchall(cursor)
            resumen = [
                {**r, "label": REF_TYPE_LABELS.get(r["ref_type"], r["ref_type"])}
                for r in rows
            ]
    except Exception as e:
        logger.error("koru_stats corp_resumen: %s", e)
        error_resumen = True

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_CORP_TOP_CONTRIBUIDORES, [inicio, fin])
            contribuidores = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats corp_contribuidores: %s", e)
    contribuidores_ant = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_CORP_TOP_CONTRIBUIDORES, [inicio_ant, fin_ant])
            contribuidores_ant = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats corp_contribuidores_ant: %s", e)
        error_contrib = True

    # Totales mes anterior para deltas %
    corp_ingresos_cat_ant, corp_gastos_cat_ant = [], []
    try:
        sql, params = _build_corp_wallet_by_category(inicio_ant, fin_ant, only_income=True)
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            corp_ingresos_cat_ant = _categorize_wallet(_fetchall(cursor), CORP_INCOME_CATEGORIES, True)
    except Exception as e:
        logger.error("koru_stats corp_ingresos_ant: %s", e)
    try:
        sql, params = _build_corp_wallet_by_category(inicio_ant, fin_ant, only_income=False)
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            corp_gastos_cat_ant = _categorize_wallet(_fetchall(cursor), CORP_EXPENSE_CATEGORIES, False)
    except Exception as e:
        logger.error("koru_stats corp_gastos_ant: %s", e)

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_CORP_INGRESOS_DIARIOS, [inicio, fin])
            ingresos_diarios = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats corp_ingresos_diarios: %s", e)
        error_diarios = True

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_CORP_TOP_SISTEMAS, [inicio, fin])
            top_sistemas = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats corp_top_sistemas: %s", e)
        error_sistemas = True

    total_ingresos = sum(float(r["total"] or 0) for r in resumen)
    total_bounties_corp = sum(
        float(r["total"] or 0) for r in resumen
        if r["ref_type"] in ("bounty_prizes", "ess_escrow_transfer")
    )
    total_donaciones = sum(
        float(r["total"] or 0) for r in resumen
        if r["ref_type"] == "player_donation"
    )
    total_industry_corp = sum(
        float(r["total"] or 0) for r in resumen
        if r["ref_type"] == "industry_job_tax"
    )

    context = {
        "mes":              date(anio, mes, 1).strftime("%B %Y"),
        **selector_ctx,
        "corp_names":       corp_names,
        "corp_ingresos_cat": corp_ingresos_cat,
        "corp_gastos_cat":   corp_gastos_cat,
        "total_ingresos_cat":     sum(c["total"] for c in corp_ingresos_cat),
        "total_gastos_cat":       sum(c["total"] for c in corp_gastos_cat),
        "total_ingresos_cat_ant": sum(c["total"] for c in corp_ingresos_cat_ant),
        "total_gastos_cat_ant":   sum(c["total"] for c in corp_gastos_cat_ant),
        "resumen":          resumen,
        "contribuidores":   _add_comparativa(contribuidores, contribuidores_ant, "total_tax"),
        "ingresos_diarios": ingresos_diarios,
        "top_sistemas":     top_sistemas,
        "total_ingresos":   total_ingresos,
        "total_bounties_corp": total_bounties_corp,
        "total_donaciones":    total_donaciones,
        "total_industry_corp": total_industry_corp,
        "error_resumen":    error_resumen,
        "error_contrib":    error_contrib,
        "error_diarios":    error_diarios,
        "error_sistemas":   error_sistemas,
        "chart_resumen": _to_json({
            "labels": [r["label"] for r in resumen[:8]],
            "data":   [float(r["total"] or 0) for r in resumen[:8]],
        }),
        "chart_contrib": _to_json({
            "labels":   [r["nombre"] for r in contribuidores],
            "bounties": [float(r["bounties"] or 0) for r in contribuidores],
            "ess":      [float(r["ess"] or 0) for r in contribuidores],
        }),
        "chart_diarios": _to_json({
            "labels":     [str(r["dia"]) for r in ingresos_diarios],
            "bounties":   [float(r["bounties"]   or 0) for r in ingresos_diarios],
            "industry":   [float(r["industry"]   or 0) for r in ingresos_diarios],
            "ded":        [float(r["ded"]        or 0) for r in ingresos_diarios],
            "donaciones": [float(r["donaciones"] or 0) for r in ingresos_diarios],
        }),
    }
    return render(request, "koru_stats/corp_dashboard.html", context)


# ---------------------------------------------------------------------------
# Moon Dashboard — constantes y queries
# ---------------------------------------------------------------------------

MOON_ORE_GROUPS = {
    1884: "Ubiquitous",
    1920: "Common",
    1921: "Uncommon",
    1922: "Rare",
    1923: "Exceptional",
}

MOON_ORE_COLORS = {
    1884: "#10b981",  # verde  — Ubiquitous
    1920: "#3b82f6",  # azul   — Common
    1921: "#8b5cf6",  # morado — Uncommon
    1922: "#f59e0b",  # naranja— Rare
    1923: "#ef4444",  # rojo   — Exceptional
}

SQL_MOON_CORP_RESUMEN = """
    SELECT ig.id   AS group_id,
           ig.name AS tier,
           SUM(ml.quantity) AS unidades,
           ROUND(SUM(ml.quantity * it.volume), 2) AS m3_total,
           ROUND(SUM(ml.quantity * COALESCE(emp.average_price, 0)), 2) AS isk_estimado
    FROM corptools_characterminingledger ml
    JOIN corptools_characteraudit          cau     ON cau.id          = ml.character_id
    JOIN eveonline_evecharacter            ec      ON ec.id           = cau.character_id
    JOIN authentication_characterownership co      ON co.character_id = ec.id
    JOIN authentication_userprofile        up      ON up.user_id      = co.user_id
    JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id
    JOIN eve_sde_itemtype                  it      ON it.id           = ml.type_name_id
    JOIN eve_sde_itemgroup                 ig      ON ig.id           = it.group_id
    LEFT JOIN eveuniverse_evemarketprice   emp     ON emp.eve_type_id = ml.type_name_id
    WHERE ig.id IN (1884, 1920, 1921, 1922, 1923)
      AND ml.date >= %s AND ml.date < %s
    GROUP BY ig.id, ig.name
    ORDER BY ig.id
"""

SQL_MOON_POR_PILOTO = """
    SELECT main_ec.character_name AS nombre,
           main_ec.character_id   AS char_id,
           main_ec.id             AS main_id,
           ig.id                  AS group_id,
           ig.name                AS tier,
           SUM(ml.quantity)       AS unidades,
           ROUND(SUM(ml.quantity * it.volume), 2) AS m3_total,
           ROUND(SUM(ml.quantity * COALESCE(emp.average_price, 0)), 2) AS isk_estimado
    FROM corptools_characterminingledger ml
    JOIN corptools_characteraudit          cau     ON cau.id          = ml.character_id
    JOIN eveonline_evecharacter            ec      ON ec.id           = cau.character_id
    JOIN authentication_characterownership co      ON co.character_id = ec.id
    JOIN authentication_userprofile        up      ON up.user_id      = co.user_id
    JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id
    JOIN eve_sde_itemtype                  it      ON it.id           = ml.type_name_id
    JOIN eve_sde_itemgroup                 ig      ON ig.id           = it.group_id
    LEFT JOIN eveuniverse_evemarketprice   emp     ON emp.eve_type_id = ml.type_name_id
    WHERE ig.id IN (1884, 1920, 1921, 1922, 1923)
      AND ml.date >= %s AND ml.date < %s
    GROUP BY main_ec.id, main_ec.character_name, main_ec.character_id, ig.id, ig.name
    ORDER BY main_ec.character_name, ig.id
"""

SQL_MOON_DETALLE_ORE = """
    SELECT main_ec.character_name AS nombre,
           main_ec.character_id   AS char_id,
           it.name                AS ore,
           ig.id                  AS group_id,
           ig.name                AS tier,
           SUM(ml.quantity)       AS unidades,
           ROUND(SUM(ml.quantity * it.volume), 2) AS m3_total,
           ROUND(SUM(ml.quantity * COALESCE(emp.average_price, 0)), 2) AS isk_estimado
    FROM corptools_characterminingledger ml
    JOIN corptools_characteraudit          cau     ON cau.id          = ml.character_id
    JOIN eveonline_evecharacter            ec      ON ec.id           = cau.character_id
    JOIN authentication_characterownership co      ON co.character_id = ec.id
    JOIN authentication_userprofile        up      ON up.user_id      = co.user_id
    JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id
    JOIN eve_sde_itemtype                  it      ON it.id           = ml.type_name_id
    JOIN eve_sde_itemgroup                 ig      ON ig.id           = it.group_id
    LEFT JOIN eveuniverse_evemarketprice   emp     ON emp.eve_type_id = ml.type_name_id
    WHERE ig.id IN (1884, 1920, 1921, 1922, 1923)
      AND ml.date >= %s AND ml.date < %s
    GROUP BY main_ec.id, main_ec.character_name, main_ec.character_id, it.id, it.name, ig.id, ig.name
    ORDER BY main_ec.character_name, ig.id, isk_estimado DESC
"""



def _generar_contrato_comprimidos(nombre, detalle_ores, rates, period_str):
    """
    Genera el texto copy-paste del contrato de comprimidos para un piloto.
    Ratio universal: 100 unidades normales = 1 comprimido.
    """
    comprimidos = {}
    total_isk = 0

    for r in detalle_ores:
        group_id = int(r["group_id"])
        tasa     = rates.get(group_id, 0)
        unidades = int(r["unidades"])
        isk      = float(r["isk_estimado"] or 0)

        tax_unidades   = int(unidades * tasa)
        tax_comprimidos = tax_unidades // 100
        tax_isk        = isk * tasa
        total_isk     += tax_isk

        if tax_comprimidos > 0:
            comp_name = f"Compressed {r['ore']}"
            comprimidos[comp_name] = comprimidos.get(comp_name, 0) + tax_comprimidos

    if not comprimidos:
        return None

    # Texto para copiar en el contrato de EVE
    lineas = [f"=== Tax Lunar Rekium — {nombre} — {period_str} ==="]
    for comp_name in sorted(comprimidos.keys()):
        cantidad = comprimidos[comp_name]
        lineas.append(f"{comp_name}: {cantidad:,}")
    lineas.append(f"--- Valor ISK referencia: {total_isk/1e6:.2f} M ISK ---")

    return {
        "texto":       "\n".join(lineas),
        "comprimidos": comprimidos,
        "total_isk":   total_isk,
    }

@permission_required("koru_stats.moon_tax_access")
def moon_dashboard(request):
    from .models import MoonTaxConfig, MoonTaxPayment
    from django.utils import timezone

    mes, anio, inicio, fin, periodo_sel = _parse_periodo(request)
    periodos_datos = _get_periodos_con_datos("luna")
    selector_ctx   = _build_selector_context(periodos_datos, periodo_sel, anio)

    # Config de tax activa
    tax_config = MoonTaxConfig.objects.filter(is_active=True).first()
    rates = tax_config.rates_by_group if tax_config else {g: 0 for g in MOON_ORE_GROUPS}

    resumen_tier, datos_piloto, detalle_ore = [], [], []
    error = False

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_MOON_CORP_RESUMEN, [inicio, fin])
            resumen_tier = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats moon_corp_resumen: %s", e)
        error = True

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_MOON_POR_PILOTO, [inicio, fin])
            datos_piloto = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats moon_por_piloto: %s", e)
        error = True

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_MOON_DETALLE_ORE, [inicio, fin])
            detalle_ore = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats moon_detalle_ore: %s", e)
        error = True

    # Agrupar por piloto y calcular tax
    pilotos = {}
    for r in datos_piloto:
        nombre  = r["nombre"]
        char_id = r["char_id"]
        main_id = r["main_id"]
        gid     = r["group_id"]
        isk     = float(r["isk_estimado"] or 0)
        tasa    = rates.get(gid, 0)
        tax     = isk * tasa

        if nombre not in pilotos:
            pilotos[nombre] = {
                "nombre":   nombre,
                "char_id":  char_id,
                "main_id":  main_id,
                "tiers":    {},
                "total_isk": 0,
                "total_tax": 0,
            }
        pilotos[nombre]["tiers"][gid] = {
            "tier":      MOON_ORE_GROUPS[gid],
            "unidades":  int(r["unidades"]),
            "m3":        float(r["m3_total"] or 0),
            "isk":       isk,
            "tasa":      tasa * 100,
            "tax":       tax,
        }
        pilotos[nombre]["total_isk"] += isk
        pilotos[nombre]["total_tax"] += tax

    pilotos_list = sorted(pilotos.values(), key=lambda x: x["total_tax"], reverse=True)

    # Crear/actualizar registros de MoonTaxPayment
    if pilotos_list and tax_config:
        period_str = f"{anio}-{mes:02d}"
        for p in pilotos_list:
            if p["total_tax"] > 0:
                MoonTaxPayment.objects.update_or_create(
                    character_id=p["main_id"],
                    period=period_str,
                    defaults={
                        "character_name": p["nombre"],
                        "isk_owed":       round(p["total_tax"], 2),
                    }
                )

    # Cargar estado de pagos
    period_str = f"{anio}-{mes:02d}"
    payments = {
        p.character_id: p
        for p in MoonTaxPayment.objects.filter(period=period_str)
    }

    for p in pilotos_list:
        pmt = payments.get(p["main_id"])
        p["is_paid"]    = pmt.is_paid if pmt else False
        p["paid_by"]    = pmt.paid_by.username if (pmt and pmt.paid_by) else None
        p["paid_at"]    = pmt.paid_at if pmt else None
        p["payment_id"] = pmt.id if pmt else None
        p["notes"]      = pmt.notes if pmt else ""

    # KPIs globales
    total_isk_moon = sum(float(r["isk_estimado"] or 0) for r in resumen_tier)
    total_tax_mes  = sum(p["total_tax"] for p in pilotos_list)
    total_pagado   = sum(p["total_tax"] for p in pilotos_list if p["is_paid"])
    total_pendiente= total_tax_mes - total_pagado

    # Detalle ore agrupado por piloto
    detalle_por_piloto = {}
    for r in detalle_ore:
        nombre = r["nombre"]
        if nombre not in detalle_por_piloto:
            detalle_por_piloto[nombre] = []
        detalle_por_piloto[nombre].append(r)
    # Generar contratos (detalle_por_piloto ya disponible)
    for p in pilotos_list:
        ores_piloto = detalle_por_piloto.get(p["nombre"], [])
        p["contrato"] = _generar_contrato_comprimidos(p["nombre"], ores_piloto, rates, period_str)

    context = {
        "mes":               date(anio, mes, 1).strftime("%B %Y"),
        **selector_ctx,
        "tax_config":        tax_config,
        "resumen_tier":      resumen_tier,
        "pilotos_list":      pilotos_list,
        "detalle_por_piloto": detalle_por_piloto,
        "total_isk_moon":    total_isk_moon,
        "total_tax_mes":     total_tax_mes,
        "total_pagado":      total_pagado,
        "total_pendiente":   total_pendiente,
        "por_luna":          por_luna,
        "moon_ore_groups":   MOON_ORE_GROUPS,
        "moon_ore_colors":   MOON_ORE_COLORS,
        "can_manage_tax":    request.user.has_perm("koru_stats.moon_tax_admin"),
        "chart_tier": _to_json({
            "labels": [MOON_ORE_GROUPS[int(r["group_id"])] for r in resumen_tier],
            "data":   [float(r["isk_estimado"] or 0) for r in resumen_tier],
            "colors": [MOON_ORE_COLORS[int(r["group_id"])] for r in resumen_tier],
        }),
        "chart_pilotos": _to_json({
            "labels": [p["nombre"] for p in pilotos_list[:10]],
            "data":   [round(p["total_tax"], 2) for p in pilotos_list[:10]],
        }),
    }
    return render(request, "koru_stats/moon_dashboard.html", context)


@permission_required("koru_stats.moon_tax_admin")
def moon_mark_paid(request, payment_id):
    from .models import MoonTaxPayment
    from django.utils import timezone
    from django.shortcuts import redirect, get_object_or_404

    if request.method != "POST":
        return redirect("koru_stats:moon_dashboard")

    payment = get_object_or_404(MoonTaxPayment, id=payment_id)
    periodo = request.POST.get("periodo", "")

    if not payment.is_paid:
        payment.is_paid  = True
        payment.paid_by  = request.user
        payment.paid_at  = timezone.now()
        payment.notes    = request.POST.get("notes", "")
        payment.save()

    return redirect(f"{request.build_absolute_uri('/koru/lunas/')}?periodo={periodo}")


# ---------------------------------------------------------------------------
# Moon Dashboard v2 — basado en moons_miningobservation (lunas de Rekium)
# ---------------------------------------------------------------------------

SQL_MOON_OBS_RESUMEN = """
    SELECT
        ig.id   AS group_id,
        ig.name AS tier,
        SUM(mo.quantity)                                               AS unidades,
        ROUND(SUM(mo.quantity * it.volume), 2)                         AS m3_total,
        ROUND(SUM(mo.quantity * COALESCE(emp.average_price, 0)), 2)    AS isk_estimado
    FROM moons_miningobservation mo
    JOIN eve_sde_itemtype              it  ON it.id           = mo.type_id
    JOIN eve_sde_itemgroup             ig  ON ig.id           = it.group_id
    LEFT JOIN eveuniverse_evemarketprice emp ON emp.eve_type_id = mo.type_id
    WHERE ig.id IN (1884, 1920, 1921, 1922, 1923)
      AND mo.last_updated >= %s AND mo.last_updated < %s
    GROUP BY ig.id, ig.name
    ORDER BY ig.id
"""

SQL_MOON_OBS_POR_PILOTO = """
    SELECT
        COALESCE(main_ec.character_name, en.name)       AS main_nombre,
        COALESCE(main_ec.character_id, mo.character_id) AS main_char_id,
        en.name                                          AS alt_nombre,
        mo.character_id                                  AS alt_char_id,
        mo.recorded_corporation_id                       AS corp_id,
        corp_en.name                                     AS corp_nombre,
        CASE WHEN mo.recorded_corporation_id = %s THEN 1 ELSE 0 END AS es_rekium,
        ig.id                                            AS group_id,
        ig.name                                          AS tier,
        SUM(mo.quantity)                                 AS unidades,
        ROUND(SUM(mo.quantity * it.volume), 2)           AS m3_total,
        ROUND(SUM(mo.quantity * COALESCE(emp.average_price, 0)), 2) AS isk_estimado
    FROM moons_miningobservation mo
    JOIN eve_sde_itemtype                it      ON it.id           = mo.type_id
    JOIN eve_sde_itemgroup               ig      ON ig.id           = it.group_id
    LEFT JOIN eveuniverse_evemarketprice emp     ON emp.eve_type_id = mo.type_id
    LEFT JOIN corptools_evename          en      ON en.eve_id       = mo.character_id
    LEFT JOIN corptools_evename          corp_en ON corp_en.eve_id  = mo.recorded_corporation_id
    LEFT JOIN eveonline_evecharacter     ec      ON ec.character_id = mo.character_id
    LEFT JOIN authentication_characterownership co ON co.character_id = ec.id
    LEFT JOIN authentication_userprofile up      ON up.user_id      = co.user_id
    LEFT JOIN eveonline_evecharacter     main_ec ON main_ec.id      = up.main_character_id
    WHERE ig.id IN (1884, 1920, 1921, 1922, 1923)
      AND mo.last_updated >= %s AND mo.last_updated < %s
    GROUP BY main_ec.character_id, main_ec.character_name,
             mo.character_id, en.name, mo.recorded_corporation_id,
             corp_en.name, ig.id, ig.name
    ORDER BY es_rekium DESC, main_nombre, alt_nombre, ig.id
"""

SQL_MOON_OBS_DETALLE_ORE = """
    SELECT
        en.name                                                            AS nombre,
        mo.character_id                                                    AS char_id,
        mo.recorded_corporation_id                                         AS corp_id,
        CASE WHEN mo.recorded_corporation_id = %s THEN 1 ELSE 0 END       AS es_rekium,
        it.name                                                            AS ore,
        ig.id                                                              AS group_id,
        ig.name                                                            AS tier,
        SUM(mo.quantity)                                                   AS unidades,
        ROUND(SUM(mo.quantity * it.volume), 2)                             AS m3_total,
        ROUND(SUM(mo.quantity * COALESCE(emp.average_price, 0)), 2)        AS isk_estimado
    FROM moons_miningobservation mo
    JOIN eve_sde_itemtype              it       ON it.id           = mo.type_id
    JOIN eve_sde_itemgroup             ig       ON ig.id           = it.group_id
    LEFT JOIN eveuniverse_evemarketprice emp    ON emp.eve_type_id = mo.type_id
    LEFT JOIN corptools_evename        en       ON en.eve_id       = mo.character_id
    WHERE ig.id IN (1884, 1920, 1921, 1922, 1923)
      AND mo.last_updated >= %s AND mo.last_updated < %s
    GROUP BY mo.character_id, en.name, mo.recorded_corporation_id,
             it.id, it.name, ig.id, ig.name
    ORDER BY es_rekium DESC, isk_estimado DESC
"""

SQL_MOON_OBS_PERIODOS = """
    SELECT DISTINCT DATE_FORMAT(mo.last_updated, '%Y-%m') AS periodo
    FROM moons_miningobservation mo
    JOIN eve_sde_itemtype  it ON it.id  = mo.type_id
    JOIN eve_sde_itemgroup ig ON ig.id  = it.group_id
    WHERE ig.id IN (1884, 1920, 1921, 1922, 1923)
    ORDER BY periodo DESC
"""


@permission_required("koru_stats.moon_tax_access")
def moon_dashboard_v2(request):
    from .models import MoonTaxConfig, MoonTaxPayment
    from django.utils import timezone

    # Períodos disponibles en moons_miningobservation
    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_MOON_OBS_PERIODOS)
            periodos_raw = [row[0] for row in cursor.fetchall()]
    except Exception:
        periodos_raw = []

    periodos_datos = []
    for valor in periodos_raw:
        try:
            anio = int(valor[:4])
            mes  = int(valor[5:7])
            periodos_datos.append({
                "valor": valor,
                "label": date(anio, mes, 1).strftime("%B"),
                "anio":  anio,
                "mes":   mes,
            })
        except (ValueError, IndexError):
            continue

    mes, anio, inicio, fin, periodo_sel = _parse_periodo(request)

    # Si el período seleccionado no tiene datos, usar el más reciente
    if periodo_sel not in [p["valor"] for p in periodos_datos] and periodos_datos:
        periodo_sel = periodos_datos[0]["valor"]
        anio = int(periodo_sel[:4])
        mes  = int(periodo_sel[5:7])
        inicio, fin = _rango_mes(anio, mes)

    selector_ctx = _build_selector_context(periodos_datos, periodo_sel, anio)

    # Corp IDs de Rekium (de TrackedCorporation)
    corp_ids = _get_corp_ids()
    rekium_corp_id = corp_ids[0] if corp_ids else 98176563

    tax_config = MoonTaxConfig.objects.filter(is_active=True).first()
    rates = tax_config.rates_by_group if tax_config else {g: 0 for g in MOON_ORE_GROUPS}

    resumen_tier, datos_piloto, detalle_ore, por_luna = [], [], [], []
    error = False

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_MOON_OBS_POR_LUNA, [inicio, fin])
            por_luna = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats moon_obs_por_luna: %s", e)

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_MOON_OBS_RESUMEN, [inicio, fin])
            resumen_tier = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats moon_obs_resumen: %s", e)
        error = True

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_MOON_OBS_POR_PILOTO, [rekium_corp_id, inicio, fin])
            datos_piloto = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats moon_obs_piloto: %s", e)
        error = True

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_MOON_OBS_DETALLE_ORE, [rekium_corp_id, inicio, fin])
            detalle_ore = _fetchall(cursor)
    except Exception as e:
        logger.error("koru_stats moon_obs_detalle: %s", e)
        error = True

    # Separar miembros (agrupados por main) y externos
    rekium   = {}  # key: main_char_id
    externos = {}  # key: alt_char_id

    for r in datos_piloto:
        gid  = int(r["group_id"])
        isk  = float(r["isk_estimado"] or 0)
        tasa = rates.get(gid, 0)
        tax  = isk * tasa

        if r["es_rekium"]:
            main_id   = r["main_char_id"]
            main_nombre = r["main_nombre"] or f"Unknown ({main_id})"
            alt_id    = r["alt_char_id"]
            alt_nombre = r["alt_nombre"] or f"Unknown ({alt_id})"

            if main_id not in rekium:
                rekium[main_id] = {
                    "nombre":    main_nombre,
                    "char_id":   r["main_char_id"],
                    "corp_id":   r["corp_id"],
                    "tiers":     {},
                    "alts":      {},
                    "total_isk": 0,
                    "total_tax": 0,
                }

            # Acumular por tier del main
            if gid not in rekium[main_id]["tiers"]:
                rekium[main_id]["tiers"][gid] = {
                    "tier":     MOON_ORE_GROUPS[gid],
                    "unidades": 0, "m3": 0, "isk": 0,
                    "tasa":     tasa * 100, "tax": 0,
                }
            rekium[main_id]["tiers"][gid]["unidades"] += int(r["unidades"])
            rekium[main_id]["tiers"][gid]["m3"]       += float(r["m3_total"] or 0)
            rekium[main_id]["tiers"][gid]["isk"]      += isk
            rekium[main_id]["tiers"][gid]["tax"]      += tax
            rekium[main_id]["total_isk"] += isk
            rekium[main_id]["total_tax"] += tax

            # Acumular por alt
            if alt_id not in rekium[main_id]["alts"]:
                rekium[main_id]["alts"][alt_id] = {
                    "nombre":    alt_nombre,
                    "char_id":   alt_id,
                    "es_main":   alt_id == main_id,
                    "tiers":     {},
                    "total_isk": 0,
                }
            if gid not in rekium[main_id]["alts"][alt_id]["tiers"]:
                rekium[main_id]["alts"][alt_id]["tiers"][gid] = {
                    "tier": MOON_ORE_GROUPS[gid], "unidades": 0, "isk": 0,
                }
            rekium[main_id]["alts"][alt_id]["tiers"][gid]["unidades"] += int(r["unidades"])
            rekium[main_id]["alts"][alt_id]["tiers"][gid]["isk"]      += isk
            rekium[main_id]["alts"][alt_id]["total_isk"]              += isk

        else:
            alt_id  = r["alt_char_id"]
            nombre  = r["alt_nombre"] or f"Unknown ({alt_id})"
            if alt_id not in externos:
                externos[alt_id] = {
                    "nombre":      nombre,
                    "char_id":     alt_id,
                    "corp_id":     r["corp_id"],
                    "corp_nombre": r["corp_nombre"] or "Unknown",
                    "tiers":       {},
                    "total_isk":   0,
                    "total_tax":   0,
                }
            if gid not in externos[alt_id]["tiers"]:
                externos[alt_id]["tiers"][gid] = {
                    "tier": MOON_ORE_GROUPS[gid], "unidades": 0, "isk": 0, "tasa": 0, "tax": 0,
                }
            externos[alt_id]["tiers"][gid]["unidades"] += int(r["unidades"])
            externos[alt_id]["tiers"][gid]["isk"]      += isk
            externos[alt_id]["total_isk"]              += isk

    rekium_list   = sorted(rekium.values(),   key=lambda x: x["total_tax"],  reverse=True)
    externos_list = sorted(externos.values(), key=lambda x: x["total_isk"], reverse=True)

    # Detalle ore por piloto
    detalle_por_piloto = {}
    for r in detalle_ore:
        cid = r["char_id"]
        if cid not in detalle_por_piloto:
            detalle_por_piloto[cid] = []
        detalle_por_piloto[cid].append(r)

    # Tax payments
    period_str = f"{anio}-{mes:02d}"
    if rekium_list and tax_config:
        for p in rekium_list:
            if p["total_tax"] > 0:
                # Intentar resolver main character
                MoonTaxPayment.objects.update_or_create(
                    character_id=p["char_id"],
                    period=period_str,
                    defaults={
                        "character_name": p["nombre"],
                        "isk_owed":       round(p["total_tax"], 2),
                    }
                )

    payments = {
        p.character_id: p
        for p in MoonTaxPayment.objects.filter(period=period_str)
    }

    for p in rekium_list:
        pmt = payments.get(p["char_id"])
        p["is_paid"]    = pmt.is_paid if pmt else False
        p["paid_by"]    = pmt.paid_by.username if (pmt and pmt.paid_by) else None
        p["paid_at"]    = pmt.paid_at if pmt else None
        p["payment_id"] = pmt.id if pmt else None
        p["notes"]      = pmt.notes if pmt else ""
        # Contrato con todos los ores de todos los alts del main
        todos_ores = []
        for alt_id in p["alts"]:
            todos_ores.extend(detalle_por_piloto.get(alt_id, []))
        p["contrato"] = _generar_contrato_comprimidos(
            p["nombre"], todos_ores, rates, period_str
        )

    # KPIs
    total_isk_moon  = sum(float(r["isk_estimado"] or 0) for r in resumen_tier)
    total_tax_mes   = sum(p["total_tax"] for p in rekium_list)
    total_pagado    = sum(p["total_tax"] for p in rekium_list if p["is_paid"])
    total_pendiente = total_tax_mes - total_pagado
    total_isk_ext   = sum(p["total_isk"] for p in externos_list)

    context = {
        "mes":               date(anio, mes, 1).strftime("%B %Y"),
        **selector_ctx,
        "tax_config":        tax_config,
        "resumen_tier":      resumen_tier,
        "rekium_list":       rekium_list,
        "externos_list":     externos_list,
        "detalle_por_piloto": detalle_por_piloto,
        "total_isk_moon":    total_isk_moon,
        "total_tax_mes":     total_tax_mes,
        "total_pagado":      total_pagado,
        "total_pendiente":   total_pendiente,
        "total_isk_ext":     total_isk_ext,
        "por_luna":          por_luna,
        "moon_ore_groups":   MOON_ORE_GROUPS,
        "moon_ore_colors":   MOON_ORE_COLORS,
        "can_manage_tax":    request.user.has_perm("koru_stats.moon_tax_admin"),
        "chart_tier": _to_json({
            "labels": [MOON_ORE_GROUPS[int(r["group_id"])] for r in resumen_tier],
            "data":   [float(r["isk_estimado"] or 0) for r in resumen_tier],
            "colors": [MOON_ORE_COLORS[int(r["group_id"])] for r in resumen_tier],
        }),
        "chart_rekium": _to_json({
            "labels": [p["nombre"] for p in rekium_list[:10]],
            "data":   [round(p["total_tax"], 2) for p in rekium_list[:10]],
        }),
    }
    return render(request, "koru_stats/moon_dashboard_v2.html", context)

SQL_MOON_OBS_POR_LUNA = """
    SELECT
        COALESCE(mn.name, CONCAT('Structure ', mo.structure_id)) AS luna,
        mo.structure_id,
        COUNT(DISTINCT mo.character_id)                                    AS mineros,
        SUM(mo.quantity)                                                   AS unidades,
        ROUND(SUM(mo.quantity * it.volume), 2)                             AS m3_total,
        ROUND(SUM(mo.quantity * COALESCE(emp.average_price, 0)), 2)        AS isk_estimado
    FROM moons_miningobservation mo
    LEFT JOIN corptools_mapsystemmoon    mn  ON mn.moon_id      = mo.moon_id
    JOIN eve_sde_itemtype               it  ON it.id            = mo.type_id
    JOIN eve_sde_itemgroup              ig  ON ig.id            = it.group_id
    LEFT JOIN eveuniverse_evemarketprice emp ON emp.eve_type_id = mo.type_id
    WHERE ig.id IN (1884, 1920, 1921, 1922, 1923)
      AND mo.last_updated >= %s AND mo.last_updated < %s
    GROUP BY mo.structure_id, mn.name
    ORDER BY isk_estimado DESC
"""


# ---------------------------------------------------------------------------
# Tendencias históricas — últimos 6 meses
# ---------------------------------------------------------------------------

SQL_TENDENCIAS_MINERIA = """
    SELECT
        DATE_FORMAT(ml.date, '%%Y-%%m') AS periodo,
        SUM(ml.quantity) AS unidades,
        ROUND(SUM(ml.quantity * COALESCE(emp.average_price, 0)), 2) AS isk_mineria
    FROM corptools_characterminingledger ml
    JOIN corptools_characteraudit          cau ON cau.id          = ml.character_id
    JOIN eveonline_evecharacter            ec  ON ec.id           = cau.character_id
    LEFT JOIN eveuniverse_evemarketprice   emp ON emp.eve_type_id = ml.type_name_id
    WHERE ml.date >= DATE_SUB(CURDATE(), INTERVAL 6 MONTH)
      AND ec.corporation_id IN ({placeholders})
    GROUP BY DATE_FORMAT(ml.date, '%%Y-%%m')
    ORDER BY periodo ASC
"""

SQL_TENDENCIAS_BOUNTIES = """
    SELECT
        DATE_FORMAT(wj.date, '%%Y-%%m') AS periodo,
        ROUND(SUM(wj.amount), 2) AS isk_bounties
    FROM corptools_corporationwalletjournalentry wj
    WHERE wj.ref_type IN ('bounty_prizes', 'ess_escrow_transfer')
      AND wj.amount > 0
      AND wj.date >= DATE_SUB(CURDATE(), INTERVAL 6 MONTH)
    GROUP BY DATE_FORMAT(wj.date, '%%Y-%%m')
    ORDER BY periodo ASC
"""


# ---------------------------------------------------------------------------
# Corp — Ingresos y Gastos por categoría
# ---------------------------------------------------------------------------

CORP_INCOME_CATEGORIES = {
    "Bounties y ESS":    ["bounty_prizes", "ess_escrow_transfer"],
    "Recompensas":       ["daily_goal_payouts", "corporate_reward_payout", "project_discovery_reward", "agent_mission_reward", "agent_mission_time_bonus_reward", "freelance_jobs_reward"],
    "Movimientos de Wallet": ["player_donation"],
    "Mercado":           ["market_transaction", "market_escrow", "reprocessing_tax"],
    "Tax Industria":     ["industry_job_tax"],
    "Contratos":         ["contract_price_payment_corp"],
    "Clones":            ["jump_clone_activation_fee", "jump_clone_installation_fee"],
    "Wallet Corp":       ["corporation_account_withdrawal"],
}

CORP_EXPENSE_CATEGORIES = {
    "Retiradas Wallet":  ["corporation_account_withdrawal"],
    "Oficinas":          ["office_rental_fee"],
    "Industria":         ["manufacturing", "reaction", "copying"],
    "Mercado":           ["market_escrow", "contract_brokers_fee_corp", "brokers_fee", "transaction_tax"],
    "Alianza":           ["alliance_maintainance_fee"],
    "Contratos":         ["contract_price_payment_corp"],
}

PERSONAL_INCOME_CATEGORIES = {
    "Bounties y ESS":    ["bounty_prizes", "ess_escrow_transfer"],
    "Mercado":           ["market_transaction", "contract_price", "market_escrow"],
    "Movimientos de Wallet": ["player_donation"],
    "Misiones":          ["agent_mission_reward", "agent_mission_time_bonus_reward"],
    "Recompensas":       ["daily_goal_payouts", "corporate_reward_payout", "project_discovery_reward"],
    "Industria":         ["manufacturing"],
    "Seguros":           ["insurance"],
}

PERSONAL_EXPENSE_CATEGORIES = {
    "Impuestos Mercado": ["transaction_tax", "brokers_fee", "market_provider_tax"],
    "Industria":         ["manufacturing", "industry_job_tax"],
    "Planetaria":        ["planetary_export_tax", "planetary_import_tax", "planetary_construction"],
    "Contratos":         ["contract_brokers_fee", "contract_price"],
    "Skills":            ["skill_purchase"],
    "Viajes":            ["structure_gate_jump", "jump_clone_activation_fee"],
    "Donaciones":        ["player_donation"],
    "Seguros":           ["insurance"],
}


def _build_corp_wallet_by_category(inicio, fin, only_income=True):
    """Agrupa el wallet de corp por categoría de ref_type."""
    sign = "> 0" if only_income else "< 0"
    sql = f"""
        SELECT ref_type,
               ROUND(SUM(amount), 2) AS total
        FROM corptools_corporationwalletjournalentry
        WHERE date >= %s AND date < %s
          AND amount {sign}
        GROUP BY ref_type
        ORDER BY ABS(SUM(amount)) DESC
    """
    return sql, [inicio, fin]


def _build_personal_wallet_by_category(corp_ids, inicio, fin, only_income=True):
    """Agrupa el wallet personal por categoría filtrando por miembros de corp."""
    placeholders = ",".join(["%s"] * len(corp_ids))
    sign = "> 0" if only_income else "< 0"
    sql = f"""
        SELECT wj.ref_type,
               ROUND(SUM(wj.amount), 2) AS total
        FROM corptools_characterwalletjournalentry wj
        JOIN corptools_characteraudit          cau     ON cau.id          = wj.character_id
        JOIN eveonline_evecharacter            ec      ON ec.id           = cau.character_id
        JOIN authentication_characterownership co      ON co.character_id = ec.id
        JOIN authentication_userprofile        up      ON up.user_id      = co.user_id
        JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id
        JOIN alumni_charactercorporationhistory h
            ON h.character_id = up.main_character_id
            AND h.corporation_id IN ({placeholders})
            AND h.start_date < %s
        LEFT JOIN alumni_charactercorporationhistory next_h
            ON next_h.character_id = h.character_id
            AND next_h.record_id = (
                SELECT MIN(record_id) FROM alumni_charactercorporationhistory
                WHERE character_id = h.character_id AND record_id > h.record_id
            )
        WHERE wj.date >= %s AND wj.date < %s
          AND wj.amount {sign}
          AND (next_h.start_date IS NULL OR next_h.start_date >= %s)
        GROUP BY wj.ref_type
        ORDER BY ABS(SUM(wj.amount)) DESC
    """
    return sql, corp_ids + [fin, inicio, fin, inicio]


def _categorize_wallet(rows, categories, only_income=True):
    """Agrupa filas de ref_type en categorías definidas."""
    # Mapa ref_type → total
    by_type = {r["ref_type"]: abs(float(r["total"] or 0)) for r in rows}

    result = []
    used = set()
    for cat_name, ref_types in categories.items():
        total = sum(by_type.get(rt, 0) for rt in ref_types)
        if total > 0:
            result.append({
                "categoria": cat_name,
                "total":     round(total, 2),
                "detalle":   [
                    {"ref_type": rt, "total": by_type[rt]}
                    for rt in ref_types if rt in by_type and by_type[rt] > 0
                ]
            })
            used.update(ref_types)

    # Otros — ref_types no categorizados
    # Otros — ref_types no categorizados
    otros = sum(v for k, v in by_type.items() if k not in used)
    if otros > 0:
        result.append({
            "categoria": "Otros",
            "total":     round(otros, 2),
            "detalle":   [
                {"ref_type": k, "total": v}
                for k, v in by_type.items() if k not in used
            ]
        })

    return sorted(result, key=lambda x: x["total"], reverse=True)


# ---------------------------------------------------------------------------
# Panel PvP / FC — requiere permiso pvp_access o fc_access
# ---------------------------------------------------------------------------

@permission_required("koru_stats.pvp_access")
def pvp_dashboard(request):
    """
    Dashboard PvP detallado. Disponible para usuarios con pvp_access (o fc_access).
    Muestra: top killers, ISK efficiency ranking, tendencia histórica.
    """
    mes, anio, inicio, fin, periodo_sel = _parse_periodo(request)
    periodos_datos = _get_periodos_con_datos("pvp")
    selector_ctx   = _build_selector_context(periodos_datos, periodo_sel, anio)
    corp_ids   = _get_corp_ids()
    corp_names = list(TrackedCorporation.objects.filter(is_active=True).values_list("corporation_name", flat=True))

    # Top killers por ISK destroyed
    top_isk_destroyed  = []
    top_ships_killed   = []
    top_efficiency     = []
    top_participations = []
    top_solo           = []
    top_damage         = []
    pvp_tendencia      = []
    error_pvp          = False

    if mes == 1:
        period_ant = f"{anio - 1}-12"
    else:
        period_ant = f"{anio}-{mes - 1:02d}"

    try:
        # Top por ISK destroyed
        top_isk_destroyed = list(
            CharacterMonthlyPvp.objects
            .filter(period=periodo_sel, corporation_id__in=corp_ids)
            .order_by("-isk_destroyed")[:15]
            .values("main_character_name", "main_character_id",
                    "ships_destroyed", "ships_lost", "isk_destroyed", "isk_lost")
        )
        for r in top_isk_destroyed:
            total = float(r["isk_destroyed"] or 0) + float(r["isk_lost"] or 0)
            r["isk_efficiency"] = round(float(r["isk_destroyed"] or 0) / total * 100, 1) if total else 0.0
            r["isk_destroyed"]  = float(r["isk_destroyed"] or 0)
            r["isk_lost"]       = float(r["isk_lost"] or 0)

        # Top por ships killed
        top_ships_killed = list(
            CharacterMonthlyPvp.objects
            .filter(period=periodo_sel, corporation_id__in=corp_ids)
            .order_by("-ships_destroyed")[:15]
            .values("main_character_name", "main_character_id",
                    "ships_destroyed", "ships_lost", "isk_destroyed")
        )
        for r in top_ships_killed:
            r["isk_destroyed"] = float(r["isk_destroyed"] or 0)

        # Top por final blows (tiros de gracia)
        top_final_blows = list(
            CharacterMonthlyPvp.objects
            .filter(period=periodo_sel, corporation_id__in=corp_ids, final_blows__gt=0)
            .order_by("-final_blows")[:15]
            .values("main_character_name", "main_character_id",
                    "final_blows", "participations", "solo_kills", "ships_destroyed")
        )

        # Top ISK efficiency (mínimo 5 kills para contar)
        top_efficiency = list(
            CharacterMonthlyPvp.objects
            .filter(period=periodo_sel, corporation_id__in=corp_ids,
                    ships_destroyed__gte=5)
            .order_by("-isk_destroyed")
        )
        eff_list = []
        for r in top_efficiency:
            total = r.isk_destroyed + r.isk_lost
            eff_pct = round(float(r.isk_destroyed) / float(total) * 100, 1) if total else 0.0
            eff_list.append({
                "main_character_name": r.main_character_name,
                "main_character_id":   r.main_character_id,
                "eff_pct":             eff_pct,
                "ships_destroyed":     r.ships_destroyed,
                "isk_destroyed":       float(r.isk_destroyed),
                "isk_lost":            float(r.isk_lost),
            })
        eff_list.sort(key=lambda r: r["eff_pct"], reverse=True)
        top_efficiency = eff_list[:10]

        # Top por participaciones
        top_participations = list(
            CharacterMonthlyPvp.objects
            .filter(period=periodo_sel, corporation_id__in=corp_ids, participations__gt=0)
            .order_by("-participations")[:15]
            .values("main_character_name", "main_character_id",
                    "participations", "ships_destroyed", "final_blows", "solo_kills")
        )

        # Top por solo kills
        top_solo = list(
            CharacterMonthlyPvp.objects
            .filter(period=periodo_sel, corporation_id__in=corp_ids, solo_kills__gt=0)
            .order_by("-solo_kills")[:15]
            .values("main_character_name", "main_character_id",
                    "solo_kills", "ships_destroyed", "isk_destroyed")
        )
        for r in top_solo:
            r["isk_destroyed"] = float(r["isk_destroyed"] or 0)

        # Top por daño infligido
        top_damage = list(
            CharacterMonthlyPvp.objects
            .filter(period=periodo_sel, corporation_id__in=corp_ids, damage_dealt__gt=0)
            .order_by("-damage_dealt")[:15]
            .values("main_character_name", "main_character_id",
                    "damage_dealt", "participations", "top_damage_kills", "final_blows")
        )
        for r in top_damage:
            r["damage_dealt"] = int(r["damage_dealt"] or 0)

        # Tendencia mensual agregada de la corp
        pvp_tendencia = _summary_pvp_tendencias(corp_ids)

    except Exception as e:
        logger.error("koru_stats pvp_dashboard: %s", e)
        error_pvp = True

    # Totales del mes (agregado directo de DB)
    totals = {}
    try:
        agg = (CharacterMonthlyPvp.objects
               .filter(period=periodo_sel, corporation_id__in=corp_ids)
               .aggregate(
                   k=Sum("ships_destroyed"), d=Sum("ships_lost"),
                   id=Sum("isk_destroyed"),  il=Sum("isk_lost"),
                   pa=Sum("participations"), so=Sum("solo_kills"),
                   fb=Sum("final_blows"),
               ))
        total_isk_all = float(agg["id"] or 0) + float(agg["il"] or 0)
        totals = {
            "total_kills":          int(agg["k"]  or 0),
            "total_deaths":         int(agg["d"]  or 0),
            "total_isk_destroyed":  float(agg["id"] or 0),
            "total_isk_lost":       float(agg["il"] or 0),
            "total_participations": int(agg["pa"] or 0),
            "total_solo_kills":     int(agg["so"] or 0),
            "total_final_blows":    int(agg["fb"] or 0),
            "corp_efficiency":      round(float(agg["id"] or 0) / total_isk_all * 100, 1) if total_isk_all else 0.0,
        }
    except Exception:
        pass

    context = {
        "mes":                date(anio, mes, 1).strftime("%B %Y"),
        **selector_ctx,
        "corp_names":         corp_names,
        "top_isk_destroyed":  top_isk_destroyed,
        "top_ships_killed":   top_ships_killed,
        "top_final_blows":    top_final_blows,
        "top_ships_killed":   top_ships_killed,
        "top_efficiency":     top_efficiency,
        "top_participations": top_participations,
        "top_solo":           top_solo,
        "top_damage":         top_damage,
        "pvp_tendencia":      pvp_tendencia,
        "totals":             totals,
        "error_pvp":          error_pvp,
        "is_fc":              request.user.has_perm("koru_stats.fc_access"),
        "chart_top_isk": _to_json({
            "labels": [r["main_character_name"] for r in top_isk_destroyed[:10]],
            "data":   [r["isk_destroyed"] for r in top_isk_destroyed[:10]],
        }),
        "chart_top_participations": _to_json({
            "labels": [r["main_character_name"] for r in top_participations[:10]],
            "data":   [int(r["participations"] or 0) for r in top_participations[:10]],
        }),
        "chart_tendencia_pvp": _to_json({
            "labels":          [r["period"] for r in pvp_tendencia],
            "isk_destroyed":   [float(r["total_isk_destroyed"] or 0) for r in pvp_tendencia],
            "isk_lost":        [float(r["total_isk_lost"] or 0) for r in pvp_tendencia],
            "ships_destroyed": [int(r["total_ships_destroyed"] or 0) for r in pvp_tendencia],
            "ships_lost":      [int(r["total_ships_lost"] or 0) for r in pvp_tendencia],
            "participations":  [int(r["total_participations"] or 0) for r in pvp_tendencia],
            "solo_kills":      [int(r["total_solo_kills"] or 0) for r in pvp_tendencia],
        }),
    }
    return render(request, "koru_stats/pvp_dashboard.html", context)

