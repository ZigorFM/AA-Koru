"""
Koru Stats — tareas Celery de agregacion.

ORDEN DE EJECUCION (importante):
  1. update_ore_prices()               — fetch precios desde Fuzzwork API
  2. aggregate_character_monthly_ore() — ore por char/mes, usando OreMarketPrice
  3. aggregate_character_monthly_summary() — resumen (ISK viene de CharacterMonthlyOre)
  4. fetch_pvp_from_zkillboard()        — PvP desde zKillboard API (zkb.totalValue)

SCHEDULE — anade esto a tu local.py:

    from celery.schedules import crontab

    # Precios + ore + summary cada 6 horas (datos frescos durante el dia)
    CELERYBEAT_SCHEDULE['koru-ore-and-summary'] = {
        'task': 'koru_stats.tasks.run_koru_ore_and_summary',
        'schedule': crontab(hour='*/6', minute=0),
    }
    # PvP una vez al dia (zkillboard API es lenta, no merece mas frecuencia)
    CELERYBEAT_SCHEDULE['koru-daily-pvp'] = {
        'task': 'koru_stats.tasks.aggregate_character_monthly_pvp',
        'schedule': crontab(hour=3, minute=30),
    }

POBLACION INICIAL — ejecuta esto UNA VEZ en el shell de Django:

    from koru_stats.tasks import run_koru_aggregations
    run_koru_aggregations(full=True)
"""

import calendar
import logging
import time
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
    CharacterKillRecord,
    OreMarketPrice,
    TrackedCorporation,
)

logger = logging.getLogger(__name__)

# IDs de minerales basicos en EVE Online
MINERAL_IDS = [34, 35, 36, 37, 38, 39, 40, 11399]
# groupID de ice en el SDE de EVE
ICE_GROUP_ID = 465
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
    """Periodos YYYY-MM: usa _all_periods_with_data como fallback (aastatistics eliminado)."""
    return _all_periods_with_data()


# ---------------------------------------------------------------------------
# Helpers zkillboard API
# ---------------------------------------------------------------------------

ZKILL_BASE    = "https://zkillboard.com/api"
ZKILL_HEADERS = {
    "Accept-Encoding": "gzip",
    "User-Agent":      "Rekium koru_stats/1.0 contact:sietehierros@gmail.com",
}


def _zkill_get(url, max_pages=5):
    """
    Descarga hasta max_pages de zkillboard. Cada pagina tiene max 200 entries.
    Devuelve lista de dicts [{killmail_id, zkb:{totalValue,...}}, ...].
    404 = sin resultados para ese filtro (no es un error).
    """
    results = []
    for page in range(1, max_pages + 1):
        paged = f"{url}page/{page}/"
        try:
            r = http_requests.get(paged, headers=ZKILL_HEADERS, timeout=20)
            if r.status_code == 429:
                logger.warning("zkill 429 — esperando 60s antes de reintentar")
                time.sleep(60)
                r = http_requests.get(paged, headers=ZKILL_HEADERS, timeout=20)
                if r.status_code == 429:
                    logger.warning("zkill 429 persistente — abortando esta URL")
                    break
            if r.status_code == 404:
                break          # sin resultados para este filtro — es normal
            if r.status_code != 200:
                logger.warning("zkill_get %s status=%s", paged, r.status_code)
                break
            if not r.text or not r.text.strip():
                break
            data = r.json()
            if not data:
                break
            results.extend(data)
            if len(data) < 200:
                break          # ultima pagina
        except Exception as exc:
            logger.warning("zkill_get %s: %s", paged, exc)
            break
        time.sleep(0.3)        # rate-limit suave
    return results


def _get_corp_characters(corp_ids):
    """
    Devuelve lista de dicts:
      {char_id, main_char_id, main_char_name, corporation_id}
    Solo personajes de miembros activos en las corps trackeadas.
    """
    if not corp_ids:
        return []
    ph = ",".join(["%s"] * len(corp_ids))
    sql = (
        "SELECT ec.character_id, main_ec.character_id, main_ec.character_name, ec.corporation_id"
        " FROM eveonline_evecharacter          ec"
        " JOIN authentication_characterownership co      ON co.character_id = ec.id"
        " JOIN authentication_userprofile        up      ON up.user_id      = co.user_id"
        " JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id"
        " WHERE ec.corporation_id IN (" + ph + ")"
        " GROUP BY ec.character_id, main_ec.character_id, main_ec.character_name, ec.corporation_id"
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, corp_ids)
            return [
                {"char_id":        row[0],
                 "main_char_id":   row[1],
                 "main_char_name": row[2],
                 "corporation_id": row[3]}
                for row in cursor.fetchall()
            ]
    except Exception as exc:
        logger.error("koru _get_corp_characters: %s", exc)
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


def _get_ice_data():
    """
    Retorna (ice_types, ice_materials, compressed_map) para tipos de hielo.
    Usa groupID=465 (Ice) de la SDE para encontrar todos los tipos de ice.
    ice_types: list de (type_id, type_name, portion_size)
    ice_materials: {type_id: [(mat_id, qty_per_unit), ...]}
    compressed_map: {raw_type_id: compressed_type_id}
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT it.id, it.name, COALESCE(it.portion_size, 1) "
                "FROM eve_sde_itemtype it "
                "JOIN eve_sde_itemgroup ig ON ig.id = it.group_id "
                "WHERE ig.id = %s AND it.published = 1 "
                "AND it.name NOT LIKE 'Compressed %%' "
                "ORDER BY it.name",
                [ICE_GROUP_ID]
            )
            rows = cursor.fetchall()
    except Exception as exc:
        logger.error("koru _get_ice_data: %s", exc)
        return [], {}, {}

    if not rows:
        logger.warning("koru _get_ice_data: no se encontraron tipos de ice en SDE (groupID=%s)", ICE_GROUP_ID)
        return [], {}, {}

    ice_types = [(int(r[0]), r[1], int(r[2] or 1)) for r in rows]
    ice_ids = [t[0] for t in ice_types]
    portion_by_id = {t[0]: t[2] for t in ice_types}

    # Compressed ice map
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT it.id, it.name FROM eve_sde_itemtype it "
                "JOIN eve_sde_itemgroup ig ON ig.id = it.group_id "
                "WHERE ig.id = %s AND it.published = 1 "
                "AND it.name LIKE 'Compressed %%'",
                [ICE_GROUP_ID]
            )
            comp_rows = cursor.fetchall()
    except Exception:
        comp_rows = []

    compressed_by_name = {r[1]: int(r[0]) for r in comp_rows}
    compressed_map = {}
    for tid, name, _ in ice_types:
        comp_name = "Compressed " + name
        if comp_name in compressed_by_name:
            compressed_map[tid] = compressed_by_name[comp_name]

    # Ice reprocessing materials
    ice_materials = {}
    if ice_ids:
        ph = ",".join(["%s"] * len(ice_ids))
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT item_type_id, material_item_type_id, quantity "
                    "FROM eve_sde_itemtypematerials "
                    "WHERE item_type_id IN (" + ph + ")",
                    ice_ids
                )
                for ore_id, mat_id, qty in cursor.fetchall():
                    ps = portion_by_id.get(ore_id, 1)
                    if qty is None:
                        continue
                    qty_per_unit = float(qty) / max(ps, 1)
                    ice_materials.setdefault(ore_id, []).append((mat_id, qty_per_unit))
        except Exception as exc:
            logger.error("koru _get_ice_data materials: %s", exc)

    return ice_types, ice_materials, compressed_map


def _get_group_ids_by_names(group_names):
    """Retorna {group_name: group_id} desde eve_sde_itemgroup."""
    if not group_names:
        return {}
    ph = ",".join(["%s"] * len(group_names))
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, name FROM eve_sde_itemgroup WHERE name IN (" + ph + ")",
                group_names
            )
            return {row[1]: int(row[0]) for row in cursor.fetchall()}
    except Exception as exc:
        logger.error("koru _get_group_ids_by_names: %s", exc)
        return {}


def _get_generic_resource_data(group_ids):
    """
    Retorna (raw_types, type_materials, compressed_map) para cualquier lista de group_ids.
    Util para moon ore, gas, y otros recursos minables.

    raw_types: [(type_id, type_name, portion_size), ...]  — excluye 'Compressed *'
    type_materials: {type_id: [(mat_id, qty_per_unit), ...]}
    compressed_map: {raw_type_id: compressed_type_id}
    """
    if not group_ids:
        return [], {}, {}
    ph = ",".join(["%s"] * len(group_ids))
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT it.id, it.name, COALESCE(it.portion_size, 1) "
                "FROM eve_sde_itemtype it "
                "WHERE it.group_id IN (" + ph + ") AND it.published = 1 "
                "ORDER BY it.name",
                group_ids
            )
            rows = cursor.fetchall()
    except Exception as exc:
        logger.error("koru _get_generic_resource_data: %s", exc)
        return [], {}, {}

    if not rows:
        return [], {}, {}

    all_types          = [(int(r[0]), r[1], int(r[2] or 1)) for r in rows]
    raw_types          = [(tid, name, ps) for tid, name, ps in all_types
                          if not name.startswith("Compressed ")]
    compressed_by_name = {name: tid for tid, name, _ in all_types
                          if name.startswith("Compressed ")}

    compressed_map = {}
    for tid, name, _ in raw_types:
        comp_name = "Compressed " + name
        if comp_name in compressed_by_name:
            compressed_map[tid] = compressed_by_name[comp_name]

    raw_ids        = [t[0] for t in raw_types]
    portion_by_id  = {t[0]: t[2] for t in raw_types}
    type_materials = {}

    if raw_ids:
        ph2 = ",".join(["%s"] * len(raw_ids))
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT item_type_id, material_item_type_id, quantity "
                    "FROM eve_sde_itemtypematerials "
                    "WHERE item_type_id IN (" + ph2 + ")",
                    raw_ids
                )
                for ore_id, mat_id, qty in cursor.fetchall():
                    ps = portion_by_id.get(ore_id, 1)
                    if qty is None:
                        continue
                    qty_per_unit = float(qty) / max(ps, 1)
                    type_materials.setdefault(ore_id, []).append((mat_id, qty_per_unit))
        except Exception as exc:
            logger.error("koru _get_generic_resource_data materials: %s", exc)

    return raw_types, type_materials, compressed_map


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

    # --- Ice ---
    ice_types, ice_materials, ice_compressed_map = _get_ice_data()
    if ice_types:
        all_ice_ids = (
            [tid for tid, _, _ in ice_types]
            + list(ice_compressed_map.values())
            + list({mat_id for mats in ice_materials.values() for mat_id, _ in mats})
        )
        ice_prices = _fetch_fuzzwork_prices(list(set(all_ice_ids)))

        for ice_id, ice_name, portion_size in ice_types:
            price_raw = ice_prices.get(ice_id, {}).get("sell", 0)

            comp_id = ice_compressed_map.get(ice_id)
            if comp_id and comp_id in ice_prices:
                comp_sell = ice_prices[comp_id].get("sell", 0)
                price_compressed = comp_sell / max(portion_size, 1)
            else:
                price_compressed = 0.0

            price_reprocessed = 0.0
            for mat_id, qty_per_unit in ice_materials.get(ice_id, []):
                mat_sell = ice_prices.get(mat_id, {}).get("sell", 0)
                price_reprocessed += qty_per_unit * 0.85 * mat_sell

            OreMarketPrice.objects.update_or_create(
                type_id=ice_id,
                defaults={
                    "type_name":         ice_name,
                    "price_raw":         round(price_raw,         4),
                    "price_compressed":  round(price_compressed,  4),
                    "price_reprocessed": round(price_reprocessed, 4),
                },
            )
            saved += 1
        logger.info("update_ore_prices: %d tipos de ice actualizados", len(ice_types))
    else:
        logger.warning("update_ore_prices: no se encontraron tipos de ice en SDE")

    # --- Moon Ore ---
    moon_group_ids = _get_group_ids_by_names([
        "Ubiquitous Moon Asteroids", "Common Moon Asteroids",
        "Uncommon Moon Asteroids", "Rare Moon Asteroids", "Exceptional Moon Asteroids",
        "Prismaticite",
    ])
    if moon_group_ids:
        moon_types, moon_materials, moon_compressed_map = _get_generic_resource_data(
            list(moon_group_ids.values())
        )
        if moon_types:
            all_moon_ids = (
                [tid for tid, _, _ in moon_types]
                + list(moon_compressed_map.values())
                + list({mat_id for mats in moon_materials.values() for mat_id, _ in mats})
            )
            moon_prices = _fetch_fuzzwork_prices(list(set(all_moon_ids)))
            for m_id, m_name, portion_size in moon_types:
                price_raw = moon_prices.get(m_id, {}).get("sell", 0)
                comp_id = moon_compressed_map.get(m_id)
                if comp_id and comp_id in moon_prices:
                    price_compressed = moon_prices[comp_id].get("sell", 0) / max(portion_size, 1)
                else:
                    price_compressed = 0.0
                price_reprocessed = sum(
                    qty_per_unit * 0.85 * moon_prices.get(mat_id, {}).get("sell", 0)
                    for mat_id, qty_per_unit in moon_materials.get(m_id, [])
                )
                OreMarketPrice.objects.update_or_create(
                    type_id=m_id,
                    defaults={
                        "type_name":         m_name,
                        "price_raw":         round(price_raw,         4),
                        "price_compressed":  round(price_compressed,  4),
                        "price_reprocessed": round(price_reprocessed, 4),
                    },
                )
                saved += 1
            logger.info("update_ore_prices: %d tipos de moon ore actualizados", len(moon_types))
    else:
        logger.warning("update_ore_prices: no se encontraron grupos de moon ore en SDE")

    # --- Gas (Harvestable Cloud + Compressed Gas en grupo separado) ---
    gas_group_ids = _get_group_ids_by_names(["Harvestable Cloud", "Compressed Gas"])
    harvestable_gid  = gas_group_ids.get("Harvestable Cloud")
    compressed_gid   = gas_group_ids.get("Compressed Gas")
    if harvestable_gid:
        gas_types, _, _ = _get_generic_resource_data([harvestable_gid])
        # Construir compressed_map desde el grupo "Compressed Gas"
        gas_compressed_map = {}
        if compressed_gid:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id, name FROM eve_sde_itemtype "
                        "WHERE group_id = %s AND published = 1",
                        [compressed_gid]
                    )
                    comp_by_name = {row[1]: int(row[0]) for row in cursor.fetchall()}
                for g_id, g_name, _ in gas_types:
                    comp_name = "Compressed " + g_name
                    if comp_name in comp_by_name:
                        gas_compressed_map[g_id] = comp_by_name[comp_name]
            except Exception as exc:
                logger.error("update_ore_prices gas compressed map: %s", exc)
        if gas_types:
            all_gas_ids = (
                [tid for tid, _, _ in gas_types]
                + list(gas_compressed_map.values())
            )
            gas_prices = _fetch_fuzzwork_prices(list(set(all_gas_ids)))
            for g_id, g_name, portion_size in gas_types:
                price_raw = gas_prices.get(g_id, {}).get("sell", 0)
                comp_id   = gas_compressed_map.get(g_id)
                if comp_id and comp_id in gas_prices:
                    price_compressed = gas_prices[comp_id].get("sell", 0) / max(portion_size, 1)
                else:
                    price_compressed = 0.0
                OreMarketPrice.objects.update_or_create(
                    type_id=g_id,
                    defaults={
                        "type_name":         g_name,
                        "price_raw":         round(price_raw,        4),
                        "price_compressed":  round(price_compressed, 4),
                        "price_reprocessed": 0.0,
                    },
                )
                saved += 1
            logger.info("update_ore_prices: %d tipos de gas actualizados (%d con precio comprimido)",
                        len(gas_types), len(gas_compressed_map))

    logger.info("update_ore_prices: %d tipos totales actualizados desde Fuzzwork", saved)
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
        "    main_ec.character_id              AS main_character_id,"
        "    MAX(main_ec.character_name)        AS main_character_name,"
        "    MAX(ec.corporation_id)             AS corporation_id,"
        "    DATE_FORMAT(wj.date, '%%Y-%%m')   AS period,"
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
            # {(main_char_id, period): {name, corp_id, bounty, ess}}
            bounty = {
                (r[0], r[3]): {
                    "main_character_name": r[1],
                    "corporation_id":      r[2],
                    "bounty_isk":          r[4] or 0,
                    "ess_isk":             r[5] or 0,
                }
                for r in cursor.fetchall()
            }

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
            m   = mining.get(key, {})
            b   = bounty.get(key, {})
            ore = ore_isk.get(key, {})

            # Usar mining como fuente principal; bounty como fallback
            # (un puro ratero no tiene fila en mining → corp_id y nombre vendrían vacíos)
            char_name = m.get("main_character_name") or b.get("main_character_name", "")
            corp_id   = m.get("corporation_id")      or b.get("corporation_id", 0)

            CharacterMonthlySummary.objects.update_or_create(
                main_character_id=char_id,
                period=period,
                defaults={
                    "main_character_name":    char_name,
                    "corporation_id":         corp_id,
                    "mining_units":           m.get("mining_units", 0) or 0,
                    "mining_m3":              m.get("mining_m3", 0) or 0,
                    "mining_isk":             float(ore.get("total_isk",   0) or 0),
                    "mining_isk_compressed":  float(ore.get("total_isk_c", 0) or 0),
                    "mining_isk_reprocessed": float(ore.get("total_isk_r", 0) or 0),
                    "bounty_isk":             b.get("bounty_isk", 0),
                    "ess_isk":                b.get("ess_isk",    0),
                },
            )

        logger.info("koru aggregate_summary: %d registros, %d periodos", len(all_keys), len(periods))
        return len(all_keys)

    except Exception as exc:
        logger.error("koru aggregate_summary error: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Tarea 3 — CharacterMonthlyPvp (zKillboard corp-level, paginado sin filtro fecha)
# ---------------------------------------------------------------------------

ESI_BASE    = "https://esi.evetech.net/latest"
ESI_HEADERS = {"User-Agent": "Rekium koru_stats/1.0 contact:sietehierros@gmail.com"}


def _esi_killmail(killmail_id, kz_hash):
    """Fetch killmail completo desde ESI. Devuelve dict o None."""
    url = f"{ESI_BASE}/killmails/{killmail_id}/{kz_hash}/"
    try:
        r = http_requests.get(url, headers=ESI_HEADERS, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        logger.warning("ESI killmail %s: %s", killmail_id, exc)
    return None


def _esi_type_name(type_id, cache={}):
    """Devuelve el nombre del tipo EVE. Cache en memoria durante la tarea."""
    if type_id in cache:
        return cache[type_id]
    if not type_id:
        return ""
    try:
        url = f"{ESI_BASE}/universe/types/{type_id}/?language=en"
        r = http_requests.get(url, headers=ESI_HEADERS, timeout=10)
        if r.status_code == 200:
            name = r.json().get("name", "")
            cache[type_id] = name
            return name
    except Exception as exc:
        logger.warning("ESI type %s: %s", type_id, exc)
    cache[type_id] = ""
    return ""



@shared_task
def fetch_pvp_from_zkillboard(periods=None, full=False):
    """
    Descarga kills/losses de zKillboard a nivel de CORP sin filtro de fecha
    (startTime/endTime eliminado por zkill). Pagina hasta cubrir los periodos
    objetivo y usa ESI para conocer fecha y personaje de cada killmail.

    Lógica:
      - Por cada página de corp kills/losses (200 por página):
        1. Fetch ESI del killmail → obtiene killmail_time y victim/attackers
        2. Filtra kills dentro de nuestros periodos objetivo
        3. Para cuando todos los kills de la página son anteriores al periodo más antiguo
    """
    from collections import defaultdict

    corp_ids = _get_active_corp_ids()
    if not corp_ids:
        logger.warning("koru fetch_pvp: sin corps activas")
        return 0

    if full:
        periods = _all_periods_with_data() or _default_periods(6)
    else:
        periods = periods or _default_periods(2)

    if not periods:
        logger.warning("koru fetch_pvp: sin periodos")
        return 0

    # Calcular rango de fechas a cubrir
    sorted_periods = sorted(periods)
    oldest_period  = sorted_periods[0]   # "YYYY-MM"
    oldest_cutoff  = oldest_period + "-01"   # "YYYY-MM-01"

    # Mapa char_id (EVE) → {main_char_id, main_char_name, corporation_id}
    characters = _get_corp_characters(corp_ids)
    if not characters:
        logger.warning("koru fetch_pvp: sin personajes en corp")
        return 0

    char_map = {c["char_id"]: c for c in characters}
    logger.info("koru fetch_pvp: %d chars, %d corps, periodos %s→%s",
                len(char_map), len(corp_ids), oldest_period, sorted_periods[-1])

    agg = defaultdict(lambda: {
        "main_char_name":   "",
        "corporation_id":   0,
        "ships_destroyed":  0,
        "ships_lost":       0,
        "isk_destroyed":    0.0,
        "isk_lost":         0.0,
        "final_blows":      0,
        "participations":   0,
        "solo_kills":       0,
        "top_damage_kills": 0,
        "damage_dealt":     0,
    })

    total_esi    = 0
    pending_names = set()   # IDs de chars/corps enemigos a resolver al final

    for corp_id in corp_ids:
        for kind in ("kills", "losses"):
            logger.info("koru fetch_pvp: corp=%s %s — paginando...", corp_id, kind)
            page = 1
            done = False

            while not done and page <= 50:   # max 50 páginas = 10 000 killmails
                url = f"{ZKILL_BASE}/{kind}/corporationID/{corp_id}/page/{page}/"
                kms = _zkill_get_single_page(url)

                if not kms:
                    break

                all_too_old = True
                for km in kms:
                    if not isinstance(km, dict):
                        continue
                    km_id   = km.get("killmail_id")
                    zkb     = km.get("zkb", {})
                    km_hash = zkb.get("hash")
                    value   = float(zkb.get("totalValue", 0) or 0)
                    if not km_id or not km_hash:
                        continue

                    esi = _esi_killmail(km_id, km_hash)
                    total_esi += 1
                    if not esi:
                        continue
                    time.sleep(0.05)

                    km_time = esi.get("killmail_time", "")   # "2026-06-07T11:26:02Z"
                    period  = km_time[:7]   # "2026-06"

                    if km_time[:10] < oldest_cutoff:
                        done = True          # este y siguientes son demasiado viejos
                        break

                    all_too_old = False

                    if period not in periods:
                        continue            # fuera del rango objetivo pero no demasiado viejo

                    km_date = km_time[:10] if km_time else None  # "2026-06-07"
                    km_hour = int(km_time[11:13]) if km_time and len(km_time) >= 13 else None  # 0-23 UTC
                    victim_ship = esi.get("victim", {}).get("ship_type_id", 0)

                    if kind == "losses":
                        victim  = esi.get("victim", {})
                        char_id = victim.get("character_id")
                        if char_id and char_id in char_map:
                            c   = char_map[char_id]
                            key = (c["main_char_id"], period)
                            agg[key]["main_char_name"] = c["main_char_name"]
                            agg[key]["corporation_id"] = c["corporation_id"]
                            agg[key]["ships_lost"]  += 1
                            agg[key]["isk_lost"]    += value
                            own_ship = victim.get("ship_type_id", 0)
                            # Capturar el piloto que dio el final blow
                            attackers   = esi.get("attackers", [])
                            final_att   = next((a for a in attackers if a.get("final_blow")), None)
                            e_char_id   = final_att.get("character_id")   if final_att else None
                            e_corp_id   = final_att.get("corporation_id") if final_att else None
                            # Acumular IDs para resolver nombres en batch al final
                            if e_char_id:
                                pending_names.add(e_char_id)
                            if e_corp_id:
                                pending_names.add(e_corp_id)
                            CharacterKillRecord.objects.update_or_create(
                                main_character_id=c["main_char_id"],
                                killmail_id=km_id,
                                defaults=dict(
                                    main_character_name=c["main_char_name"],
                                    period=period,
                                    is_loss=True,
                                    ship_type_id=own_ship,
                                    ship_name=_esi_type_name(own_ship),
                                    value_isk=value,
                                    kill_date=km_date,
                                    kill_hour=km_hour,
                                    final_blow=False,
                                    solo=False,
                                    enemy_char_id=e_char_id,
                                    enemy_corp_id=e_corp_id,
                                ),
                            )

                    else:
                        attackers  = esi.get("attackers", [])
                        is_solo    = zkb.get("solo", False)
                        max_dmg    = max((a.get("damage_done", 0) for a in attackers), default=0)
                        for att in attackers:
                            char_id = att.get("character_id")
                            if not char_id or char_id not in char_map:
                                continue
                            c   = char_map[char_id]
                            key = (c["main_char_id"], period)
                            agg[key]["main_char_name"] = c["main_char_name"]
                            agg[key]["corporation_id"] = c["corporation_id"]
                            agg[key]["participations"]  += 1
                            dmg = att.get("damage_done", 0)
                            agg[key]["damage_dealt"]    += dmg
                            got_final = att.get("final_blow", False)
                            if got_final:
                                agg[key]["ships_destroyed"] += 1
                                agg[key]["isk_destroyed"]   += value
                                agg[key]["final_blows"]     += 1
                                if is_solo:
                                    agg[key]["solo_kills"]  += 1
                            elif dmg == max_dmg and max_dmg > 0:
                                agg[key]["top_damage_kills"] += 1
                            CharacterKillRecord.objects.update_or_create(
                                main_character_id=c["main_char_id"],
                                killmail_id=km_id,
                                defaults=dict(
                                    main_character_name=c["main_char_name"],
                                    period=period,
                                    is_loss=False,
                                    ship_type_id=victim_ship,
                                    ship_name=_esi_type_name(victim_ship),
                                    value_isk=value,
                                    kill_date=km_date,
                                    kill_hour=km_hour,
                                    final_blow=got_final,
                                    solo=is_solo and got_final,
                                ),
                            )

                if all_too_old:
                    done = True

                page += 1
                time.sleep(0.5)   # pausa entre páginas zkill

            logger.info("koru fetch_pvp: corp=%s %s — %d páginas, %d calls ESI",
                        corp_id, kind, page - 1, total_esi)

    # ── Resolver nombres de enemigos vía ESI ──
    name_map = {}   # {eve_id: (name, category)}
    if pending_names:
        try:
            ids_list = list(pending_names)
            for i in range(0, len(ids_list), 1000):
                chunk = ids_list[i:i + 1000]
                r = http_requests.post(
                    "https://esi.evetech.net/latest/universe/names/?datasource=tranquility",
                    json=chunk,
                    timeout=20,
                )
                if r.status_code == 200:
                    for entry in r.json():
                        name_map[entry["id"]] = (entry["name"], entry.get("category", ""))
        except Exception as exc:
            logger.warning("koru fetch_pvp: ESI names batch falló: %s", exc)

    # Actualizar enemy_char_name y enemy_corp_name en los KillRecords recién creados
    if name_map:
        from django.db.models import Q
        ids_with_names = list(name_map.keys())
        records_to_update = CharacterKillRecord.objects.filter(
            Q(enemy_char_id__in=ids_with_names) | Q(enemy_corp_id__in=ids_with_names),
            is_loss=True,
        ).only("id", "enemy_char_id", "enemy_corp_id", "enemy_char_name", "enemy_corp_name")
        bulk = []
        for rec in records_to_update:
            changed = False
            if rec.enemy_char_id and rec.enemy_char_id in name_map and not rec.enemy_char_name:
                rec.enemy_char_name = name_map[rec.enemy_char_id][0]
                changed = True
            if rec.enemy_corp_id and rec.enemy_corp_id in name_map and not rec.enemy_corp_name:
                rec.enemy_corp_name = name_map[rec.enemy_corp_id][0]
                changed = True
            if changed:
                bulk.append(rec)
        if bulk:
            CharacterKillRecord.objects.bulk_update(bulk, ["enemy_char_name", "enemy_corp_name"])
            logger.info("koru fetch_pvp: %d enemy names actualizados", len(bulk))

    # ── Persistir ──
    saved = 0
    for (main_char_id, period), data in agg.items():
        if not data["main_char_name"]:
            continue
        CharacterMonthlyPvp.objects.update_or_create(
            main_character_id=main_char_id,
            period=period,
            defaults={
                "main_character_name": data["main_char_name"],
                "corporation_id":      data["corporation_id"],
                "ships_destroyed":     data["ships_destroyed"],
                "ships_lost":          data["ships_lost"],
                "isk_destroyed":       round(data["isk_destroyed"], 2),
                "isk_lost":            round(data["isk_lost"],      2),
                "final_blows":         data["final_blows"],
                "participations":      data["participations"],
                "solo_kills":          data["solo_kills"],
                "top_damage_kills":    data["top_damage_kills"],
                "damage_dealt":        data["damage_dealt"],
            },
        )
        saved += 1

    logger.info("koru fetch_pvp DONE: %d registros guardados, %d calls ESI", saved, total_esi)
    return saved


def _zkill_get_single_page(url):
    """Fetch una sola página de zkillboard. Devuelve lista o []."""
    try:
        r = http_requests.get(url, headers=ZKILL_HEADERS, timeout=20)
        if r.status_code == 429:
            logger.warning("zkill 429 — esperando 60s")
            time.sleep(60)
            r = http_requests.get(url, headers=ZKILL_HEADERS, timeout=20)
            if r.status_code == 429:
                logger.warning("zkill 429 persistente — abortando")
                return []
        if r.status_code == 404:
            return []
        if r.status_code != 200 or not r.text.strip():
            logger.warning("zkill_get_single_page %s status=%s", url, r.status_code)
            return []
        data = r.json()
        if isinstance(data, dict) and "error" in data:
            logger.warning("zkill error: %s", data["error"])
            return []
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("zkill_get_single_page %s: %s", url, exc)
        return []


# Alias para compatibilidad con run_koru_aggregations
aggregate_character_monthly_pvp = fetch_pvp_from_zkillboard


# ---------------------------------------------------------------------------
# Tareas coordinadoras
# ---------------------------------------------------------------------------

@shared_task
def run_koru_ore_and_summary(full=False):
    """
    Ejecuta solo precios + ore + summary (sin PvP).
    Diseñada para ejecutarse cada 6 horas para mantener datos frescos.
    """
    logger.info("koru run_koru_ore_and_summary START (full=%s)", full)

    try:
        n_prices = update_ore_prices()
    except Exception as exc:
        logger.error("koru ore_and_summary: update_ore_prices fallo: %s\n%s", exc, traceback.format_exc())
        n_prices = 0

    n_ore     = aggregate_character_monthly_ore(full=full)
    n_summary = aggregate_character_monthly_summary(full=full)

    logger.info(
        "koru run_koru_ore_and_summary DONE — prices=%s, ore=%s, summary=%s",
        n_prices, n_ore, n_summary,
    )
    return n_summary


@shared_task
def run_koru_aggregations(full=False):
    """
    Ejecuta todas las agregaciones en orden correcto:
      1. update_ore_prices   — fetch Fuzzwork
      2. aggregate_ore       — ore por char/mes con los 3 ISK
      3. aggregate_summary   — resumen (ISK desde CharacterMonthlyOre)
      4. fetch_pvp           — PvP desde zKillboard API

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

# Alias para compatibilidad con run_koru_aggregations
aggregate_character_monthly_pvp = fetch_pvp_from_zkillboard


# ---------------------------------------------------------------------------
# Tareas coordinadoras
# ---------------------------------------------------------------------------

@shared_task
def run_koru_ore_and_summary(full=False):
    """
    Ejecuta solo precios + ore + summary (sin PvP).
    Diseñada para ejecutarse cada 6 horas para mantener datos frescos.
    """
    logger.info("koru run_koru_ore_and_summary START (full=%s)", full)

    try:
        n_prices = update_ore_prices()
    except Exception as exc:
        logger.error("koru ore_and_summary: update_ore_prices fallo: %s\n%s", exc, traceback.format_exc())
        n_prices = 0

    n_ore     = aggregate_character_monthly_ore(full=full)
    n_summary = aggregate_character_monthly_summary(full=full)

    logger.info(
        "koru run_koru_ore_and_summary DONE — prices=%s, ore=%s, summary=%s",
        n_prices, n_ore, n_summary,
    )
    return n_summary


@shared_task
def run_koru_aggregations(full=False):
    """
    Ejecuta todas las agregaciones en orden correcto.
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


# ===========================================================================
# AUDITOR — Fase 1 (señales sobre datos ya existentes)
# Schedule sugerido en local.py:
#   CELERYBEAT_SCHEDULE['koru-auditor-daily'] = {
#       'task': 'koru_stats.tasks.run_auditor_pipeline',
#       'schedule': crontab(hour=4, minute=15),   # tras koru-daily-pvp (3:30)
#   }
# ===========================================================================

def _auditor_clamp(v, lo=0, hi=100):
    try:
        return max(lo, min(hi, int(round(v))))
    except (TypeError, ValueError):
        return lo


@shared_task
def sync_blue_standings():
    """Cachea blue/own ids leyendo los States del core de AA en AuditorConfig."""
    from .models import AuditorConfig
    cfg, _ = AuditorConfig.objects.get_or_create(tag="default")
    blue_states = list(cfg.blue_state_ids or [])
    own_states  = list(cfg.own_state_ids or [])

    # Autodetección por nombre del State si no están configurados a mano
    if not blue_states or not own_states:
        with connection.cursor() as c:
            c.execute("SELECT id, name FROM authentication_state")
            rows = c.fetchall()
        for sid, name in rows:
            n = (name or "").lower()
            if not cfg.blue_state_ids and ("aliad" in n or "blue" in n):
                if sid not in blue_states:
                    blue_states.append(sid)
            if not cfg.own_state_ids and ("miembro" in n or "member" in n or "academy" in n):
                if sid not in own_states:
                    own_states.append(sid)

    def _alliances(state_ids):
        if not state_ids:
            return []
        ph = ",".join(["%s"] * len(state_ids))
        with connection.cursor() as c:
            c.execute(
                "SELECT DISTINCT ai.alliance_id "
                "FROM authentication_state_member_alliances sma "
                "JOIN eveonline_eveallianceinfo ai ON ai.id = sma.eveallianceinfo_id "
                f"WHERE sma.state_id IN ({ph})", state_ids)
            return [r[0] for r in c.fetchall()]

    def _corps(state_ids):
        if not state_ids:
            return []
        ph = ",".join(["%s"] * len(state_ids))
        with connection.cursor() as c:
            c.execute(
                "SELECT DISTINCT ci.corporation_id "
                "FROM authentication_state_member_corporations smc "
                "JOIN eveonline_evecorporationinfo ci ON ci.id = smc.evecorporationinfo_id "
                f"WHERE smc.state_id IN ({ph})", state_ids)
            return [r[0] for r in c.fetchall()]

    cfg.blue_state_ids    = blue_states
    cfg.own_state_ids     = own_states
    cfg.blue_alliance_ids = _alliances(blue_states)
    cfg.blue_corp_ids     = _corps(blue_states)
    cfg.own_alliance_ids  = _alliances(own_states)
    cfg.own_corp_ids      = _corps(own_states)
    cfg.save()
    logger.info("koru sync_blue_standings: blue=%s (%s ally/%s corp) own=%s",
                blue_states, len(cfg.blue_alliance_ids), len(cfg.blue_corp_ids), own_states)
    return {"blue_states": blue_states, "blue_alliances": cfg.blue_alliance_ids, "own_states": own_states}


@shared_task
def compute_auditor_scores(periods=None, full=False):
    """Calcula scores de riesgo por main/periodo.

    F1 dims: PvP, Espías (del periodo) + Fuga, Huecos (estado ACTUAL).
    Fuga/Huecos solo puntúan en el periodo en curso; en periodos pasados
    el riesgo se calcula solo con las dimensiones de comportamiento (PvP, Espías).
    """
    from collections import defaultdict
    from .models import AuditorConfig, AuditorRiskScore, CharacterMonthlyPvp, CharacterKillRecord

    corp_ids = _get_active_corp_ids()
    if not corp_ids:
        logger.warning("koru auditor: sin corps activas")
        return 0
    periods = _all_periods_with_data() if full else (periods or _default_periods(1))
    current_period = datetime.now().strftime("%Y-%m")
    cfg, _ = AuditorConfig.objects.get_or_create(tag="default")
    stale_days = int(cfg.token_stale_dias or 7)
    inact_days = max(1, int(cfg.inactividad_dias or 14))
    ph = ",".join(["%s"] * len(corp_ids))

    # Salud de visibilidad + último login + corp por main (estado ACTUAL, no del periodo)
    #   n_blind = personajes sin audit o con wallet sin sincronizar > stale_days
    #             (token caducado/revocado = perdimos visibilidad).
    #   OJO: la columna 'active' de corptools NO indica salud de token (91% es 0 aunque sincronicen).
    with connection.cursor() as c:
        c.execute(
            "SELECT main_ec.character_id, MAX(main_ec.character_name), MAX(main_ec.corporation_id), "
            "       MAX(cau.last_known_login), COUNT(DISTINCT ec.id) AS n_chars, "
            "       SUM(CASE WHEN cau.id IS NULL OR cau.last_update_wallet IS NULL "
            "                 OR cau.last_update_wallet < (NOW() - INTERVAL %s DAY) "
            "                THEN 1 ELSE 0 END) AS n_blind, "
            "       DATEDIFF(NOW(), MAX(cau.last_known_login)) AS dias_login "
            "FROM eveonline_evecharacter ec "
            "JOIN authentication_characterownership co ON co.character_id = ec.id "
            "JOIN authentication_userprofile up ON up.user_id = co.user_id "
            "JOIN eveonline_evecharacter main_ec ON main_ec.id = up.main_character_id "
            "LEFT JOIN corptools_characteraudit cau ON cau.character_id = ec.id "
            f"WHERE ec.corporation_id IN ({ph}) "
            "GROUP BY main_ec.character_id", [stale_days] + corp_ids)
        base = {}
        for r in c.fetchall():
            base[r[0]] = {
                "main_name": r[1] or "", "corp_id": r[2] or 0, "last_login": r[3],
                "n_chars": int(r[4] or 0), "n_blind": int(r[5] or 0),
                "dias_login": (int(r[6]) if r[6] is not None else None),
            }
    main_ids = list(base.keys())
    saved = 0

    for period in periods:
        is_current = (period == current_period)
        pvp = {p.main_character_id: p for p in CharacterMonthlyPvp.objects.filter(period=period)}
        loss_by_main = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
        for mid, ecorp, isk in CharacterKillRecord.objects.filter(
                period=period, is_loss=True, main_character_id__in=main_ids
        ).values_list("main_character_id", "enemy_corp_id", "value_isk"):
            if not ecorp:
                continue  # muertes sin corp enemiga (NPC/no resuelto) no cuentan para concentracion
            cell = loss_by_main[mid][ecorp]
            cell[0] += 1
            cell[1] += float(isk or 0)

        for mid, info in base.items():
            det = {}
            # FUGA (estado actual — solo puntúa en el periodo en curso)
            if is_current:
                dias = info["dias_login"]
                if dias is None:
                    score_fuga = 50
                    det["dias_sin_login"] = None
                else:
                    score_fuga = _auditor_clamp(dias / inact_days * 50)
                    det["dias_sin_login"] = dias
            else:
                score_fuga = 0
            # HUECOS (estado actual — solo puntúa en el periodo en curso)
            n_chars = info["n_chars"] or 1
            if is_current:
                score_huecos = _auditor_clamp(100 * info["n_blind"] / n_chars)
                det["chars_ciegos"] = info["n_blind"]
                det["n_chars"] = info["n_chars"]
            else:
                score_huecos = 0
            # ESPÍAS (del periodo)
            enemy = loss_by_main.get(mid, {})
            total_losses = sum(v[0] for v in enemy.values())
            if total_losses >= 3:
                herf = sum((v[0] / total_losses) ** 2 for v in enemy.values())
                score_espias = _auditor_clamp(herf * 100)
                top = max(enemy.items(), key=lambda kv: kv[1][0])
                det["losses"] = total_losses
                det["concentracion"] = round(herf, 2)
                det["top_enemy_corp_id"] = top[0]
                det["top_enemy_losses"] = top[1][0]
            else:
                score_espias = 0
                det["losses"] = total_losses
            # PvP (ineficiencia/feeding) — señal débil en F1
            p = pvp.get(mid)
            if p:
                eff = p.isk_efficiency
                act = float(p.isk_destroyed) + float(p.isk_lost)
                score_pvp = _auditor_clamp((50 - eff) * 2) if act >= 100_000_000 else 0
                det["isk_efficiency"] = round(eff, 1)
            else:
                score_pvp = 0
            # Ciclo y financiero: aún no calculados en F1
            score_ciclo = 0
            score_financiero = 0
            # Total renormalizado sobre las dimensiones activas del periodo
            w_pvp = float(cfg.w_pvp); w_esp = float(cfg.w_espias)
            w_fug = float(cfg.w_fuga); w_hue = float(cfg.w_huecos)
            if is_current:
                sw = (w_pvp + w_esp + w_fug + w_hue) or 1
                risk = (w_pvp * score_pvp + w_esp * score_espias +
                        w_fug * score_fuga + w_hue * score_huecos) / sw
                dims_act = ["pvp", "espias", "fuga", "huecos"]
            else:
                sw = (w_pvp + w_esp) or 1
                risk = (w_pvp * score_pvp + w_esp * score_espias) / sw
                dims_act = ["pvp", "espias"]
            risk = _auditor_clamp(risk)
            if risk >= cfg.umbral_rojo:
                nivel = "rojo"
            elif risk >= cfg.umbral_naranja:
                nivel = "naranja"
            elif risk >= cfg.umbral_amarillo:
                nivel = "amarillo"
            else:
                nivel = "verde"
            det["_dims_activas"] = dims_act
            det["_periodo_actual"] = is_current
            AuditorRiskScore.objects.update_or_create(
                main_character_id=mid, period=period,
                defaults=dict(
                    main_character_name=info["main_name"], corporation_id=info["corp_id"],
                    score_pvp=score_pvp, score_ciclo=score_ciclo, score_espias=score_espias,
                    score_fuga=score_fuga, score_huecos=score_huecos, score_financiero=score_financiero,
                    risk_total=risk, nivel=nivel, detalle=det,
                ),
            )
            saved += 1
    logger.info("koru compute_auditor_scores: %s scores (periodos=%s)", saved, periods)
    return saved


@shared_task
def emit_auditor_alerts(periods=None):
    """Genera AuditorAlert en BD para dimensiones >= umbral_naranja. Discord = Fase 4."""
    from .models import AuditorConfig, AuditorRiskScore, AuditorAlert
    cfg, _ = AuditorConfig.objects.get_or_create(tag="default")
    periods = periods or _default_periods(1)
    created = 0
    dims = [
        ("score_huecos",  "huecos",  "cobertura",            "Cobertura/tokens del piloto en riesgo"),
        ("score_fuga",    "fuga",    "inactividad",          "Caída de actividad / posible fuga"),
        ("score_espias",  "espias",  "concentracion_enemigo","Pérdidas concentradas ante una corp"),
        ("score_pvp",     "pvp",     "pvp_anomalo",          "Comportamiento PvP anómalo"),
    ]
    for s in AuditorRiskScore.objects.filter(period__in=periods):
        for field, fam, code, titulo in dims:
            val = getattr(s, field)
            if val >= cfg.umbral_naranja:
                sev = "critico" if val >= cfg.umbral_rojo else "warn"
                AuditorAlert.objects.update_or_create(
                    main_character_id=s.main_character_id, period=s.period, codigo=code,
                    defaults=dict(
                        main_character_name=s.main_character_name, familia=fam,
                        severidad=sev, titulo=titulo,
                        detalle={"score": val, "risk_total": s.risk_total},
                    ),
                )
                created += 1
    logger.info("koru emit_auditor_alerts: %s alertas (calibracion=%s)", created, cfg.modo_calibracion)
    return created


@shared_task
def run_auditor_pipeline(full=False):
    """Pipeline diario del Auditor: standings -> scores -> alertas."""
    logger.info("koru run_auditor_pipeline START (full=%s)", full)
    try:
        sync_blue_standings()
    except Exception as exc:
        logger.error("koru auditor sync_blue_standings: %s\n%s", exc, traceback.format_exc())
    n_scores = compute_auditor_scores(full=full)
    try:
        n_alerts = emit_auditor_alerts()
    except Exception as exc:
        logger.error("koru auditor emit_auditor_alerts: %s", exc)
        n_alerts = 0
    logger.info("koru run_auditor_pipeline DONE — scores=%s alerts=%s", n_scores, n_alerts)
    return {"scores": n_scores, "alerts": n_alerts}
