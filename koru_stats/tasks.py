"""
Koru Stats — tareas Celery de agregación.

Las vistas del dashboard hacen JOINs pesados en tiempo real.
Estas tareas pre-agregan los datos en CharacterMonthlySummary y
CharacterMonthlyOre para que las vistas puedan hacer queries simples.

SCHEDULE — añade esto a tu local.py:

    from celery.schedules import crontab
    CELERYBEAT_SCHEDULE['koru-daily-aggregations'] = {
        'task': 'koru_stats.tasks.run_koru_aggregations',
        'schedule': crontab(hour=3, minute=0),
    }

POBLACIÓN INICIAL — ejecuta esto UNA VEZ en el shell de Django:

    from koru_stats.tasks import run_koru_aggregations
    run_koru_aggregations(full=True)
"""

import logging
from datetime import datetime

from celery import shared_task
from django.db import connection

from .models import CharacterMonthlyOre, CharacterMonthlySummary, TrackedCorporation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _get_active_corp_ids():
    ids = list(
        TrackedCorporation.objects
        .filter(is_active=True)
        .values_list("corporation_id", flat=True)
    )
    return ids if ids else []


def _default_periods(n=2):
    """Últimos N meses en formato YYYY-MM."""
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
    Todos los períodos YYYY-MM con datos en corptools.
    Sin parámetros → usamos %Y-%m directamente (PyMySQL no escapa sin args).
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


def _period_to_yyyymm(period):
    """'2026-05' → 202605 (int). Usado para comparar sin DATE_FORMAT + params."""
    anio, mes = period.split("-")
    return int(anio) * 100 + int(mes)


# ---------------------------------------------------------------------------
# Tarea 1 — CharacterMonthlySummary
# ---------------------------------------------------------------------------

@shared_task
def aggregate_character_monthly_summary(periods=None, full=False):
    """
    Agrega mining + bounties + ESS por personaje principal por mes.

    Args:
        periods: Lista de strings 'YYYY-MM'. Si None, usa los últimos 2 meses.
        full:    Si True, agrega TODOS los períodos disponibles.
    """
    corp_ids = _get_active_corp_ids()
    if not corp_ids:
        logger.warning("koru aggregate_summary: no hay corps activas configuradas")
        return 0

    periods = _all_periods_with_data() if full else (periods or _default_periods())
    if not periods:
        logger.warning("koru aggregate_summary: no hay períodos con datos")
        return 0

    # Convertimos períodos a enteros YYYYMM para evitar DATE_FORMAT con params
    # (DATE_FORMAT usa %, que conflicta con los placeholders %s de PyMySQL)
    yyyymm_list = [_period_to_yyyymm(p) for p in periods]

    ph_corps   = ",".join(["%s"] * len(corp_ids))
    ph_periods = ",".join(["%s"] * len(yyyymm_list))
    params     = corp_ids + yyyymm_list

    # ── Mining ──────────────────────────────────────────────────────────────
    sql_mining = f"""
        SELECT
            main_ec.character_id   AS main_character_id,
            main_ec.character_name AS main_character_name,
            ec.corporation_id,
            DATE_FORMAT(ml.date, '%%Y-%%m')                              AS period,
            SUM(ml.quantity)                                              AS mining_units,
            ROUND(SUM(ml.quantity * it.volume), 2)                       AS mining_m3,
            ROUND(SUM(ml.quantity * COALESCE(emp.average_price, 0)), 2)  AS mining_isk
        FROM corptools_characterminingledger ml
        JOIN corptools_characteraudit          cau     ON cau.id          = ml.character_id
        JOIN eveonline_evecharacter            ec      ON ec.id           = cau.character_id
        JOIN authentication_characterownership co      ON co.character_id = ec.id
        JOIN authentication_userprofile        up      ON up.user_id      = co.user_id
        JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id
        JOIN eve_sde_itemtype                  it      ON it.id           = ml.type_name_id
        LEFT JOIN eveuniverse_evemarketprice   emp     ON emp.eve_type_id = ml.type_name_id
        WHERE ec.corporation_id IN ({ph_corps})
          AND (YEAR(ml.date) * 100 + MONTH(ml.date)) IN ({ph_periods})
        GROUP BY main_ec.character_id, main_ec.character_name,
                 ec.corporation_id, DATE_FORMAT(ml.date, '%%Y-%%m')
    """

    # ── Bounties + ESS ──────────────────────────────────────────────────────
    sql_bounty = f"""
        SELECT
            main_ec.character_id AS main_character_id,
            DATE_FORMAT(wj.date, '%%Y-%%m') AS period,
            SUM(CASE WHEN wj.ref_type = 'bounty_prizes'       THEN wj.amount ELSE 0 END) AS bounty_isk,
            SUM(CASE WHEN wj.ref_type = 'ess_escrow_transfer' THEN wj.amount ELSE 0 END) AS ess_isk
        FROM corptools_characterwalletjournalentry wj
        JOIN corptools_characteraudit          cau     ON cau.id          = wj.character_id
        JOIN eveonline_evecharacter            ec      ON ec.id           = cau.character_id
        JOIN authentication_characterownership co      ON co.character_id = ec.id
        JOIN authentication_userprofile        up      ON up.user_id      = co.user_id
        JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id
        WHERE wj.ref_type IN ('bounty_prizes', 'ess_escrow_transfer')
          AND wj.amount > 0
          AND ec.corporation_id IN ({ph_corps})
          AND (YEAR(wj.date) * 100 + MONTH(wj.date)) IN ({ph_periods})
        GROUP BY main_ec.character_id, DATE_FORMAT(wj.date, '%%Y-%%m')
    """

    try:
        with connection.cursor() as cursor:
            cursor.execute(sql_mining, params)
            cols   = [c[0] for c in cursor.description]
            mining = {(r[0], r[3]): dict(zip(cols, r)) for r in cursor.fetchall()}

        with connection.cursor() as cursor:
            cursor.execute(sql_bounty, params)
            bounty = {(r[0], r[1]): (r[2] or 0, r[3] or 0) for r in cursor.fetchall()}

        all_keys = set(mining) | set(bounty)
        for key in all_keys:
            char_id, period = key
            m = mining.get(key, {})
            b_isk, e_isk = bounty.get(key, (0, 0))
            CharacterMonthlySummary.objects.update_or_create(
                main_character_id=char_id,
                period=period,
                defaults={
                    "main_character_name": m.get("main_character_name", ""),
                    "corporation_id":      m.get("corporation_id", 0),
                    "mining_units":        m.get("mining_units", 0) or 0,
                    "mining_m3":           m.get("mining_m3", 0) or 0,
                    "mining_isk":          m.get("mining_isk", 0) or 0,
                    "bounty_isk":          b_isk,
                    "ess_isk":             e_isk,
                },
            )

        logger.info(
            "koru aggregate_summary: %d registros, %d períodos",
            len(all_keys), len(periods),
        )
        return len(all_keys)

    except Exception as exc:
        logger.error("koru aggregate_summary error: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Tarea 2 — CharacterMonthlyOre
# ---------------------------------------------------------------------------

@shared_task
def aggregate_character_monthly_ore(periods=None, full=False):
    """
    Agrega desglose de ore por personaje principal, mes y tipo de mineral.
    """
    corp_ids = _get_active_corp_ids()
    if not corp_ids:
        return 0

    periods = _all_periods_with_data() if full else (periods or _default_periods())
    if not periods:
        return 0

    yyyymm_list = [_period_to_yyyymm(p) for p in periods]

    ph_corps   = ",".join(["%s"] * len(corp_ids))
    ph_periods = ",".join(["%s"] * len(yyyymm_list))

    sql = f"""
        SELECT
            main_ec.character_id   AS main_character_id,
            ec.corporation_id,
            DATE_FORMAT(ml.date, '%%Y-%%m') AS period,
            it.id                  AS type_id,
            it.name                AS type_name,
            SUM(ml.quantity)                                              AS quantity,
            ROUND(SUM(ml.quantity * it.volume), 2)                       AS m3,
            ROUND(SUM(ml.quantity * COALESCE(emp.average_price, 0)), 2)  AS isk
        FROM corptools_characterminingledger ml
        JOIN corptools_characteraudit          cau     ON cau.id          = ml.character_id
        JOIN eveonline_evecharacter            ec      ON ec.id           = cau.character_id
        JOIN authentication_characterownership co      ON co.character_id = ec.id
        JOIN authentication_userprofile        up      ON up.user_id      = co.user_id
        JOIN eveonline_evecharacter            main_ec ON main_ec.id      = up.main_character_id
        JOIN eve_sde_itemtype                  it      ON it.id           = ml.type_name_id
        LEFT JOIN eveuniverse_evemarketprice   emp     ON emp.eve_type_id = ml.type_name_id
        WHERE ec.corporation_id IN ({ph_corps})
          AND (YEAR(ml.date) * 100 + MONTH(ml.date)) IN ({ph_periods})
        GROUP BY main_ec.character_id, ec.corporation_id,
                 DATE_FORMAT(ml.date, '%%Y-%%m'), it.id, it.name
    """

    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, corp_ids + yyyymm_list)
            rows = cursor.fetchall()

        for row in rows:
            CharacterMonthlyOre.objects.update_or_create(
                main_character_id=row[0],
                period=row[2],
                type_id=row[3],
                defaults={
                    "corporation_id": row[1],
                    "type_name":      row[4],
                    "quantity":       row[5] or 0,
                    "m3":             row[6] or 0,
                    "isk":            row[7] or 0,
                },
            )

        logger.info(
            "koru aggregate_ore: %d registros, %d períodos",
            len(rows), len(periods),
        )
        return len(rows)

    except Exception as exc:
        logger.error("koru aggregate_ore error: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Tarea coordinadora — esta es la que se schedula
# ---------------------------------------------------------------------------

@shared_task
def run_koru_aggregations(full=False):
    """
    Ejecuta todas las agregaciones en secuencia.

    Uso normal (diario): run_koru_aggregations.delay()
    Población inicial:   run_koru_aggregations(full=True)
    """
    logger.info("koru run_koru_aggregations START (full=%s)", full)
    n_summary = aggregate_character_monthly_summary(full=full)
    n_ore     = aggregate_character_monthly_ore(full=full)
    logger.info(
        "koru run_koru_aggregations DONE — summary=%s, ore=%s",
        n_summary, n_ore,
    )
