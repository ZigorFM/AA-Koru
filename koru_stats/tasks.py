"""
Koru Stats — tareas Celery de agregacion.

ORDEN DE EJECUCION (importante):
  1. update_ore_prices()               — fetch precios desde Fuzzwork API
  2. aggregate_character_monthly_ore() — ore por char/mes, usando OreMarketPrice
  3. aggregate_character_monthly_summary() — resumen (ISK viene de CharacterMonthlyOre)
  4. aggregate_character_monthly_pvp() — PvP desde aastatistics_zkillmonth

SCHEDULE — anade esto a tu local.py:

    from celery.schedules import crontab
    CELERYBEAT_SCHEDULE['koru-daily-aggregations'] = {
        'task': 'koru_stats.tasks.run_koru_aggregations',
        'schedule': crontab(hour=3, minute=0),
    }

POBLACION INICIAL — ejecuta esto UNA VEZ en el shell de Django:

    from koru_stats.tasks import run_koru_aggregations
    run_koru_aggregations(full=True)
"""

import logging
import traceback
from datetime import datetime

import requests as http_requests
from celery import shared_task
from django.db import connection
from django.db.models import Sum as OrmSum

from .models import (
    CharacterMonthlyOre,
    CharacterMonthlySummary,
    CharacterMonthlyPvp,
    OreMarketPrice,
    TrackedCorporation,
)

logger = logging.getLogger(__name__)

# IDs de minerales basicos en EVE Online
MINERAL_IDS = [34, 35, 36, 37, 38, 39, 40, 11399]
# Region The Forge (Jita) para precios de referencia
FUZZWORK_REGION = 10000002


# ---------------------------------------------------------------------------
# Helpers internos — corp / periodo
# ---------------------------------------------------------------------------

def _get_active_corp_ids():
    ids = list(
        TrackedCorporation.objects
        .filter(is_active=True)
        .values_list("corporation_id", flat=True)
    )
    return ids if ids else []


def _default_periods(n=2):
    """Ultimos N meses en formato YYYY-MM."""
    hoy = datetime.now()
    result = []
    for i in range(n):
        mes = hoy.month - i
        anio = hoy.year
        if mes <= 0:
            mes += 12
            anio -= 1
        result.append(f"{anio}-{mes:02d}")
    return result


def _all_periods_with_data():
    """
    Todos los periodos YYYY-MM con datos en corptools.
    Sin parametros: usamos %Y-%m directamente (PyMySQL no escapa sin args).
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT DISTINCT periodo FROM (
                    SELECT DATE_FORMAT(date, '%Y-%m') AS periodo
                    FROM corptools_characterminingledger
                    UNION
                    SELECT DATE_FORMAT(date, '%Y-%m') AS periodo
                    FROM corptools_characterwalletjournalentry
                    WHERE ref_type IN ('bounty_prizes', 'ess_escrow_transfer')
                ) t
                ORDER BY periodo ASC
            """)
            return [row[0] for row in cursor.fetchall()]
    except Exception as exc:
        logger.error("koru _all_periods_with_data: %s", exc)
        return []


def _all_pvp_periods():
    """Todos los periodos YYYY-MM disponibles en aastatistics_zkillmonth."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT DISTINCT CONCAT(year, '-', LPAD(month, 2, '0')) AS periodo
                FROM aastatistics_zkillmonth
                ORDER BY periodo ASC
            """)
            return [row[0] for row in cursor.fetchall()]
    except Exception as exc:
        logger.error("koru _all_pvp_periods: %s", exc)
        return []


def _period_to_yyyymm(period):
    """'2026-05' -> 202605 (int). Usado para comparar sin DATE_FORMAT + params."""
    anio, mes = period.split("-")
    return int(anio) * 100 + int(mes)


# ---------------------------------------------------------------------------
# Helpers de precios — Fuzzwork Market API
# ---------------------------------------------------------------------------

def _fetch_fuzzwork_prices(type_ids):
    """
    Fetch precios desde Fuzzwork Market API.
    Retorna {type_id: {'buy': float, 'sell': float}}.
    """
    if not type_ids:
        return {}

    result = {}
    chunk_size = 200

    for i in range(0, len(type_ids), chunk_size):
        chunk = type_ids[i : i + chunk_size]
        url = (
            "https://market.fuzzwork.co.uk/aggregates/"
            "?region=" + str(FUZZWORK_REGION) +
            "&types=" + ",".join(str(t) for t in chunk)
        )
        try:
            resp = http_requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for tid_str, prices in data.items():
                tid = int(tid_str)
                result[tid] = {
                    "buy":  float((prices.get("buy")  or {}).get("max", 0) or 0),
                    "sell": float((prices.get("sell") or {}).get("min", 0) or 0),
                }
        except Exception as exc:
            logger.error("koru _fetch_fuzzwork_prices chunk %d: %s", i, exc)

    return result


def _get_ore_data():
    """
    Retorna (raw_ores, ore_materials, compressed_map).

    raw_ores: list de (type_id, type_name, portion_size)  — solo ores RAW
    ore_materials: {type_id: [(mat_id, qty_per_unit), ...]}
    compressed_map: {raw_type_id: compressed_type_id}

    Intenta leer portionSize de eve_sde_itemtype.
    Si la columna no existe, asume 100 (correcto para la mayoria de ores k-space).
    """
    mineral_ids_str = ",".join(str(m) for m in MINERAL_IDS)

    # Intentar con 'portionSize' (CamelCase del EVE SDE original)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT it.id, it.name, COALESCE(it.portionSize, 100) "
                "FROM eve_sde_itemtype it "
                "JOIN eve_sde_itemtypematerials itm ON itm.item_type_id = it.id "
                "WHERE itm.material_item_type_id IN (" + mineral_ids_str + ") "
                "AND it.published = 1 "
                "ORDER BY it.name"
            )
            rows = cursor.fetchall()
    except Exception:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DISTINCT it.id, it.name "
                    "FROM eve_sde_itemtype it "
                    "JOIN eve_sde_itemtypematerials itm ON itm.item_type_id = it.id "
                    "WHERE itm.material_item_type_id IN (" + mineral_ids_str + ") "
                    "AND it.published = 1 "
                    "ORDER BY it.name"
                )
                rows = [(r[0], r[1], 100) for r in cursor.fetchall()]
        except Exception as exc2:
            logger.error("koru _get_ore_data: %s", exc2)
            return [], {}, {}

    if not rows:
        return [], {}, {}

    all_ore_types = [(int(r[0]), r[1], int(r[2] or 100)) for r in rows]

    # Separar raw y comprimidos
    raw_ores = [(tid, name, ps) for tid, name, ps in all_ore_types
                if not name.startswith("Compressed ")]
    compressed_by_name = {name: tid for tid, name, _ in all_ore_types
                          if name.startswith("Compressed ")}

    compressed_map = {}
    for tid, name, _ in raw_ores:
        comp_name = "Compressed " + name
        if comp_name in compressed_by_name:
            compressed_map[tid] = compressed_by_name[comp_name]

    raw_ids = [r[0] for r in raw_ores]
    portion_by_id = {r[0]: r[2] for r in raw_ores}
    ore_materials = {}

    if raw_ids:
        ph = ",".join(["%s"] * len(raw_ids))
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT item_type_id, material_item_type_id, quantity "
                "FROM eve_sde_itemtypematerials "
                "WHERE item_type_id IN (" + ph + ") "
                "AND material_item_type_id IN (" + mineral_ids_str + ")",
                raw_ids
            )
            for ore_id, mat_id, qty in cursor.fetchall():
                ps = portion_by_id.get(ore_id, 100)
                if qty is None:
                    continue
                qty_per_unit = float(qty) / max(ps, 1)
                ore_materials.setdefault(ore_id, []).append((mat_id, qty_per_unit))

    return raw_ores, ore_materials, compressed_map


# ---------------------------------------------------------------------------
# Tarea 0 — OreMarketPrice (precios desde Fuzzwork)
# ---------------------------------------------------------------------------

@shared_task
def update_ore_prices():
    """
    Actualiza OreMarketPrice con precios de Fuzzwork Market API.

    Precios almacenados (ISK por unidad de ore RAW):
      price_raw:         precio market sell por unidad sin comprimir
      price_compressed:  precio equivalente si se vende comprimido
                         (compressed_sell / portion_size, ratio tipico 100:1)
      price_reprocessed: ISK por unidad al reprocesar al 85%
                         (Sum mat_qty_per_unit x 0.85 x mineral_sell)

    Debe ejecutarse ANTES de aggregate_character_monthly_ore.
    """
    raw_ores, ore_materials, compressed_map = _get_ore_data()
    if not raw_ores:
        logger.warning("update_ore_prices: no se encontraron ores en SDE")
        return 0

    all_ids = (
        [tid for tid, _, _ in raw_ores]
        + list(compressed_map.values())
        + MINERAL_IDS
    )
    prices = _fetch_fuzzwork_prices(list(set(all_ids)))

    if not prices:
        logger.error("update_ore_prices: Fuzzwork no devolvio precios")
        return 0

    saved = 0
    for ore_id, ore_name, portion_size in raw_ores:
        price_raw = prices.get(ore_id, {}).get("sell", 0)

        comp_id = compressed_map.get(ore_id)
        if comp_id and comp_id in prices:
            comp_sell = prices[comp_id].get("sell", 0)
            price_compressed = comp_sell / max(portion_size, 1)
        else:
            price_compressed = 0.0

        price_reprocessed = 0.0
        for mat_id, qty_per_unit in ore_materials.get(ore_id, []):
            mineral_sell = prices.get(mat_id, {}).get("sell", 0)
            price_reprocessed += qty_per_unit * 0.85 * mineral_sell

        OreMarketPrice.objects.update_or_create(
            type_id=ore_id,
            defaults={
                "type_name":         ore_name,
                "price_raw":         round(price_raw,         4),
                "price_compressed":  round(price_compressed,  4),
                "price_reprocessed": round(price_reprocessed, 4),
            },
        )
        saved += 1

    logger.info("update_ore_prices: %d tipos actualizados desde Fuzzwork", saved)
    return saved


# ---------------------------------------------------------------------------
# Tarea 1 — CharacterMonthlyOre (con 3 valoraciones de ISK)
# ---------------------------------------------------------------------------

@shared_task
def aggregate_character_monthly_ore(periods=None, full=False):
    """
    Agrega desglose de ore por personaje principal, mes y tipo de mineral.
    Calcula isk / isk_compressed / isk_reprocessed usando OreMarketPrice.
    Debe ejecutarse DESPUES de update_ore_prices.
    """
    corp_ids = _get_active_corp_ids()
    if not corp_ids:
        return 0

    periods = _all_periods_with_data() if full else (periods or _default_periods())
    if not periods:
        return 0

    yyyymm_list = [_period_to_yyyymm(p) for p in periods]
    ph_corps    = ",".join(["%s"] * len(corp_ids))
    ph_periods  = ",".join(["%s"] * len(yyyymm_list))

    sql = (
        "SELECT"
        "    main_ec.character_id   AS main_character_id,"
        "    ec.corporation_id,"
        "    DATE_FORMAT(ml.date, '%%Y-%%m') AS period,"
        "    it.id                  AS type_id,"
        "    it.name                AS type_name,"
        "    SUM(ml.quantity)                       AS quantity,"
        "    ROUND(SUM(ml.quantity * it.volume), 2) AS m3"
        " FROM corptools_characterminingledger ml"
        " JOIN corptools_characteraudit          cau     ON cau.id          = ml.character_id"
        " JOIN eveonline_evecharacter            ec      ON ec.id           = cau.character_id"
        " JOIN authentication_characterownership co      ON co.character_id = ec.id"
        " JOIN authentication_userprofile        up      ON up.user_id      = co.user_id"
        " JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id"
        " JOIN eve_sde_itemtype                  it      ON it.id           = ml.type_name_id"
        " WHERE ec.corporation_id IN (" + ph_corps + ")"
        " AND (YEAR(ml.date) * 100 + MONTH(ml.date)) IN (" + ph_periods + ")"
        " GROUP BY main_ec.character_id, ec.corporation_id,"
        "          DATE_FORMAT(ml.date, '%%Y-%%m'), it.id, it.name"
    )

    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, corp_ids + yyyymm_list)
            rows = cursor.fetchall()

        price_map = {p.type_id: p for p in OreMarketPrice.objects.all()}

        for row in rows:
            main_char_id = row[0]
            corp_id      = row[1]
            period       = row[2]
            type_id      = row[3]
            type_name    = row[4]
            qty          = int(row[5] or 0)
            m3           = float(row[6] or 0)

            price_info = price_map.get(type_id)
            if price_info:
                isk      = round(qty * float(price_info.price_raw),         2)
                isk_comp = round(qty * float(price_info.price_compressed),  2)
                isk_repr = round(qty * float(price_info.price_reprocessed), 2)
            else:
                isk = isk_comp = isk_repr = 0.0

            CharacterMonthlyOre.objects.update_or_create(
                main_character_id=main_char_id,
                period=period,
                type_id=type_id,
                defaults={
                    "corporation_id":  corp_id,
                    "type_name":       type_name,
                    "quantity":        qty,
                    "m3":              m3,
                    "isk":             isk,
                    "isk_compressed":  isk_comp,
                    "isk_reprocessed": isk_repr,
                },
            )

        logger.info("koru aggregate_ore: %d registros, %d periodos", len(rows), len(periods))
        return len(rows)

    except Exception as exc:
        logger.error("koru aggregate_ore error: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Tarea 2 — CharacterMonthlySummary (ISK desde CharacterMonthlyOre)
# ---------------------------------------------------------------------------

@shared_task
def aggregate_character_monthly_summary(periods=None, full=False):
    """
    Agrega mining + bounties + ESS por personaje principal por mes.
    Los ISK de mining vienen de CharacterMonthlyOre (3 valoraciones).
    Debe ejecutarse DESPUES de aggregate_character_monthly_ore.
    """
    corp_ids = _get_active_corp_ids()
    if not corp_ids:
        logger.warning("koru aggregate_summary: no hay corps activas configuradas")
        return 0

    periods = _all_periods_with_data() if full else (periods or _default_periods())
    if not periods:
        logger.warning("koru aggregate_summary: no hay periodos con datos")
        return 0

    yyyymm_list = [_period_to_yyyymm(p) for p in periods]
    ph_corps    = ",".join(["%s"] * len(corp_ids))
    ph_periods  = ",".join(["%s"] * len(yyyymm_list))
    params      = corp_ids + yyyymm_list

    sql_mining = (
        "SELECT"
        "    main_ec.character_id   AS main_character_id,"
        "    main_ec.character_name AS main_character_name,"
        "    ec.corporation_id,"
        "    DATE_FORMAT(ml.date, '%%Y-%%m') AS period,"
        "    SUM(ml.quantity)                       AS mining_units,"
        "    ROUND(SUM(ml.quantity * it.volume), 2) AS mining_m3"
        " FROM corptools_characterminingledger ml"
        " JOIN corptools_characteraudit          cau     ON cau.id          = ml.character_id"
        " JOIN eveonline_evecharacter            ec      ON ec.id           = cau.character_id"
        " JOIN authentication_characterownership co      ON co.character_id = ec.id"
        " JOIN authentication_userprofile        up      ON up.user_id      = co.user_id"
        " JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id"
        " JOIN eve_sde_itemtype                  it      ON it.id           = ml.type_name_id"
        " WHERE ec.corporation_id IN (" + ph_corps + ")"
        " AND (YEAR(ml.date) * 100 + MONTH(ml.date)) IN (" + ph_periods + ")"
        " GROUP BY main_ec.character_id, main_ec.character_name,"
        "          ec.corporation_id, DATE_FORMAT(ml.date, '%%Y-%%m')"
    )

    sql_bounty = (
        "SELECT"
        "    main_ec.character_id AS main_character_id,"
        "    DATE_FORMAT(wj.date, '%%Y-%%m') AS period,"
        "    SUM(CASE WHEN wj.ref_type = 'bounty_prizes'       THEN wj.amount ELSE 0 END) AS bounty_isk,"
        "    SUM(CASE WHEN wj.ref_type = 'ess_escrow_transfer' THEN wj.amount ELSE 0 END) AS ess_isk"
        " FROM corptools_characterwalletjournalentry wj"
        " JOIN corptools_characteraudit          cau     ON cau.id          = wj.character_id"
        " JOIN eveonline_evecharacter            ec      ON ec.id           = cau.character_id"
        " JOIN authentication_characterownership co      ON co.character_id = ec.id"
        " JOIN authentication_userprofile        up      ON up.user_id      = co.user_id"
        " JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id"
        " WHERE wj.ref_type IN ('bounty_prizes', 'ess_escrow_transfer')"
        " AND wj.amount > 0"
        " AND ec.corporation_id IN (" + ph_corps + ")"
        " AND (YEAR(wj.date) * 100 + MONTH(wj.date)) IN (" + ph_periods + ")"
        " GROUP BY main_ec.character_id, DATE_FORMAT(wj.date, '%%Y-%%m')"
    )

    try:
        with connection.cursor() as cursor:
            cursor.execute(sql_mining, params)
            cols   = [c[0] for c in cursor.description]
            mining = {(r[0], r[3]): dict(zip(cols, r)) for r in cursor.fetchall()}

        with connection.cursor() as cursor:
            cursor.execute(sql_bounty, params)
            bounty = {(r[0], r[1]): (r[2] or 0, r[3] or 0) for r in cursor.fetchall()}

        ore_isk_qs = (
            CharacterMonthlyOre.objects
            .filter(period__in=periods, corporation_id__in=corp_ids)
            .values("main_character_id", "period")
            .annotate(
                total_isk=OrmSum("isk"),
                total_isk_c=OrmSum("isk_compressed"),
                total_isk_r=OrmSum("isk_reprocessed"),
            )
        )
        ore_isk = {
            (r["main_character_id"], r["period"]): r
            for r in ore_isk_qs
        }

        all_keys = set(mining) | set(bounty)
        for key in all_keys:
            char_id, period = key
            m            = mining.get(key, {})
            b_isk, e_isk = bounty.get(key, (0, 0))
            ore          = ore_isk.get(key, {})

            CharacterMonthlySummary.objects.update_or_create(
                main_character_id=char_id,
                period=period,
                defaults={
                    "main_character_name":    m.get("main_character_name", ""),
                    "corporation_id":         m.get("corporation_id", 0),
                    "mining_units":           m.get("mining_units", 0) or 0,
                    "mining_m3":              m.get("mining_m3", 0) or 0,
                    "mining_isk":             float(ore.get("total_isk",   0) or 0),
                    "mining_isk_compressed":  float(ore.get("total_isk_c", 0) or 0),
                    "mining_isk_reprocessed": float(ore.get("total_isk_r", 0) or 0),
                    "bounty_isk":             b_isk,
                    "ess_isk":                e_isk,
                },
            )

        logger.info("koru aggregate_summary: %d registros, %d periodos", len(all_keys), len(periods))
        return len(all_keys)

    except Exception as exc:
        logger.error("koru aggregate_summary error: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Tarea 3 — CharacterMonthlyPvp (desde aastatistics_zkillmonth)
# ---------------------------------------------------------------------------

@shared_task
def aggregate_character_monthly_pvp(periods=None, full=False):
    """
    Agrega estadisticas PvP mensuales por personaje principal.
    Fuente: aastatistics_zkillmonth (actualizado por aastatistics).
    """
    corp_ids = _get_active_corp_ids()
    if not corp_ids:
        return 0

    if full:
        periods = _all_pvp_periods()
    else:
        periods = periods or _default_periods()

    if not periods:
        logger.warning("koru aggregate_pvp: no hay periodos disponibles")
        return 0

    yyyymm_list = [_period_to_yyyymm(p) for p in periods]
    ph_corps    = ",".join(["%s"] * len(corp_ids))
    ph_periods  = ",".join(["%s"] * len(yyyymm_list))
    params      = corp_ids + yyyymm_list

    sql = (
        "SELECT"
        "    main_ec.character_id                           AS main_character_id,"
        "    main_ec.character_name                         AS main_character_name,"
        "    ec.corporation_id,"
        "    CONCAT(zk.year, '-', LPAD(zk.month, 2, '0')) AS period,"
        "    SUM(zk.ships_destroyed)                       AS ships_destroyed,"
        "    SUM(zk.ships_lost)                            AS ships_lost,"
        "    SUM(zk.isk_destroyed)                         AS isk_destroyed,"
        "    SUM(zk.isk_lost)                              AS isk_lost"
        " FROM aastatistics_zkillmonth zk"
        " JOIN eveonline_evecharacter            ec      ON ec.character_id = zk.char_id"
        " JOIN authentication_characterownership co      ON co.character_id = ec.id"
        " JOIN authentication_userprofile        up      ON up.user_id      = co.user_id"
        " JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id"
        " WHERE ec.corporation_id IN (" + ph_corps + ")"
        " AND (zk.year * 100 + zk.month) IN (" + ph_periods + ")"
        " GROUP BY main_ec.character_id, main_ec.character_name, ec.corporation_id, period"
    )

    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()

        for row in rows:
            CharacterMonthlyPvp.objects.update_or_create(
                main_character_id=row[0],
                period=row[3],
                defaults={
                    "main_character_name": row[1],
                    "corporation_id":      row[2],
                    "ships_destroyed":     int(row[4] or 0),
                    "ships_lost":          int(row[5] or 0),
                    "isk_destroyed":       row[6] or 0,
                    "isk_lost":            row[7] or 0,
                },
            )

        logger.info("koru aggregate_pvp: %d registros, %d periodos", len(rows), len(periods))
        return len(rows)

    except Exception as exc:
        logger.error("koru aggregate_pvp error: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Tarea coordinadora — esta es la que se schedula
# ---------------------------------------------------------------------------

@shared_task
def run_koru_aggregations(full=False):
    """
    Ejecuta todas las agregaciones en orden correcto:
      1. update_ore_prices   — fetch Fuzzwork
      2. aggregate_ore       — ore por char/mes con los 3 ISK
      3. aggregate_summary   — resumen (ISK desde CharacterMonthlyOre)
      4. aggregate_pvp       — PvP desde aastatistics_zkillmonth

    Uso normal (diario): run_koru_aggregations.delay()
    Poblacion inicial:   run_koru_aggregations(full=True)
    """
    logger.info("koru run_koru_aggregations START (full=%s)", full)

    try:
        n_prices = update_ore_prices()
    except Exception as exc:
        logger.error("koru run_koru_aggregations: update_ore_prices fallo: %s\n%s", exc, traceback.format_exc())
        n_prices = 0

    n_ore     = aggregate_character_monthly_ore(full=full)
    n_summary = aggregate_character_monthly_summary(full=full)

    try:
        n_pvp = aggregate_character_monthly_pvp(full=full)
    except Exception as exc:
        logger.warning("koru run_koru_aggregations: PvP skipped: %s", exc)
        n_pvp = 0

    logger.info(
        "koru run_koru_aggregations DONE — prices=%s, ore=%s, summary=%s, pvp=%s",
        n_prices, n_ore, n_summary, n_pvp,
    )
