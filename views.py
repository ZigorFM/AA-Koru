import json
import logging
import calendar
from decimal import Decimal
from datetime import date, datetime

from django.contrib.auth.decorators import login_required
from django.db import connection
from django.shortcuts import render

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _DecimalEncoder(json.JSONEncoder):
    """Permite serializar Decimal (tipo que devuelve MariaDB) a JSON."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def _fetchall(cursor):
    """Convierte el resultado de un cursor en lista de dicts."""
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _to_json(data):
    return json.dumps(data, cls=_DecimalEncoder)


def _rango_mes(año, mes):
    """
    Devuelve (fecha_inicio, fecha_fin) para el mes dado como strings.
    Usar rangos explícitos en lugar de YEAR()/MONTH() permite que
    MariaDB aproveche los índices en columnas date/datetime.
    Ejemplo: ('2026-06-01', '2026-07-01')
    """
    ultimo_dia = calendar.monthrange(año, mes)[1]
    inicio = date(año, mes, 1)
    # El día siguiente al último día del mes → condición < fin
    if mes == 12:
        fin = date(año + 1, 1, 1)
    else:
        fin = date(año, mes + 1, 1)
    return str(inicio), str(fin)


# ---------------------------------------------------------------------------
# SQL — tabla de ownership correcta: authentication_characterownership
#       rangos de fecha explícitos para usar índices
# ---------------------------------------------------------------------------

SQL_TOP_MINEROS = """
    SELECT
        main_ec.character_name  AS nombre,
        main_ec.character_id    AS char_id,
        SUM(ml.quantity)        AS total_unidades
    FROM corptools_characterminingledger ml
    JOIN corptools_characteraudit           cau     ON cau.id           = ml.character_id
    JOIN eveonline_evecharacter             ec      ON ec.id            = cau.character_id
    JOIN authentication_characterownership  co      ON co.character_id  = ec.id
    JOIN authentication_userprofile         up      ON up.user_id       = co.user_id
    JOIN eveonline_evecharacter             main_ec ON main_ec.id       = up.main_character_id
    WHERE ml.date >= %s
      AND ml.date  < %s
    GROUP BY main_ec.id, main_ec.character_name, main_ec.character_id
    ORDER BY total_unidades DESC
    LIMIT 10
"""

SQL_TOP_BOUNTIES = """
    SELECT
        main_ec.character_name  AS nombre,
        main_ec.character_id    AS char_id,
        SUM(wj.amount)          AS total_isk
    FROM corptools_characterwalletjournalentry wj
    JOIN corptools_characteraudit           cau     ON cau.id           = wj.character_id
    JOIN eveonline_evecharacter             ec      ON ec.id            = cau.character_id
    JOIN authentication_characterownership  co      ON co.character_id  = ec.id
    JOIN authentication_userprofile         up      ON up.user_id       = co.user_id
    JOIN eveonline_evecharacter             main_ec ON main_ec.id       = up.main_character_id
    WHERE wj.ref_type = 'bounty_prizes'
      AND wj.date    >= %s
      AND wj.date     < %s
    GROUP BY main_ec.id, main_ec.character_name, main_ec.character_id
    ORDER BY total_isk DESC
    LIMIT 10
"""

SQL_MINING_PERSONAL = """
    SELECT
        ec.character_name   AS nombre,
        ec.character_id     AS char_id,
        SUM(ml.quantity)    AS total_unidades
    FROM corptools_characterminingledger ml
    JOIN corptools_characteraudit           cau ON cau.id           = ml.character_id
    JOIN eveonline_evecharacter             ec  ON ec.id            = cau.character_id
    JOIN authentication_characterownership  co  ON co.character_id  = ec.id
    JOIN authentication_userprofile         up  ON up.user_id       = co.user_id
    WHERE up.main_character_id = %s
      AND ml.date >= %s
      AND ml.date  < %s
    GROUP BY ec.id, ec.character_name, ec.character_id
    ORDER BY total_unidades DESC
"""

SQL_BOUNTIES_DIARIOS = """
    SELECT
        DATE(wj.date)       AS dia,
        SUM(wj.amount)      AS total_isk
    FROM corptools_characterwalletjournalentry wj
    JOIN corptools_characteraudit           cau ON cau.id           = wj.character_id
    JOIN eveonline_evecharacter             ec  ON ec.id            = cau.character_id
    JOIN authentication_characterownership  co  ON co.character_id  = ec.id
    JOIN authentication_userprofile         up  ON up.user_id       = co.user_id
    WHERE up.main_character_id = %s
      AND wj.ref_type = 'bounty_prizes'
      AND wj.date    >= %s
      AND wj.date     < %s
    GROUP BY DATE(wj.date)
    ORDER BY dia ASC
"""


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

@login_required
def dashboard(request):
    """
    Dashboard global de la corp:
    - Top 10 mineros del mes actual
    - Top 10 bounties del mes actual
    """
    hoy = datetime.now()
    inicio, fin = _rango_mes(hoy.year, hoy.month)

    top_mineros  = []
    top_bounties = []
    error_mineros  = False
    error_bounties = False

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_TOP_MINEROS, [inicio, fin])
            top_mineros = _fetchall(cursor)
    except Exception as e:
        logger.error("rekstats dashboard: error top_mineros: %s", e)
        error_mineros = True

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_TOP_BOUNTIES, [inicio, fin])
            top_bounties = _fetchall(cursor)
    except Exception as e:
        logger.error("rekstats dashboard: error top_bounties: %s", e)
        error_bounties = True

    chart_mineros = _to_json({
        "labels": [r["nombre"] for r in top_mineros],
        "data":   [int(r["total_unidades"]) for r in top_mineros],
    })
    chart_bounties = _to_json({
        "labels": [r["nombre"] for r in top_bounties],
        "data":   [float(Decimal(str(r["total_isk"]))) for r in top_bounties],
    })

    context = {
        "mes":            hoy.strftime("%B %Y"),
        "top_mineros":    top_mineros,
        "top_bounties":   top_bounties,
        "chart_mineros":  chart_mineros,
        "chart_bounties": chart_bounties,
        "error_mineros":  error_mineros,
        "error_bounties": error_bounties,
    }
    return render(request, "rekstats/dashboard.html", context)


@login_required
def mi_dashboard(request):
    """
    Dashboard personal del piloto logueado.
    Muestra datos de todos sus personajes (main + alts).
    """
    try:
        main = request.user.profile.main_character
    except Exception:
        main = None

    if not main:
        return render(request, "rekstats/mi_dashboard.html", {"sin_main": True})

    hoy    = datetime.now()
    inicio, fin = _rango_mes(hoy.year, hoy.month)
    main_id = main.id  # eveonline_evecharacter.id

    mining_personal  = []
    bounties_diarios = []
    error_mining    = False
    error_bounties  = False

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_MINING_PERSONAL, [main_id, inicio, fin])
            mining_personal = _fetchall(cursor)
    except Exception as e:
        logger.error("rekstats personal: error mining: %s", e)
        error_mining = True

    try:
        with connection.cursor() as cursor:
            cursor.execute(SQL_BOUNTIES_DIARIOS, [main_id, inicio, fin])
            bounties_diarios = _fetchall(cursor)
    except Exception as e:
        logger.error("rekstats personal: error bounties: %s", e)
        error_bounties = True

    total_minado   = sum(int(r["total_unidades"]) for r in mining_personal)
    total_bounties = sum(float(Decimal(str(r["total_isk"]))) for r in bounties_diarios)

    chart_mining_personal = _to_json({
        "labels": [r["nombre"] for r in mining_personal],
        "data":   [int(r["total_unidades"]) for r in mining_personal],
    })
    chart_bounties_dia = _to_json({
        "labels": [str(r["dia"]) for r in bounties_diarios],
        "data":   [float(Decimal(str(r["total_isk"]))) for r in bounties_diarios],
    })

    context = {
        "main":                  main,
        "mes":                   hoy.strftime("%B %Y"),
        "mining_personal":       mining_personal,
        "bounties_diarios":      bounties_diarios,
        "total_minado":          total_minado,
        "total_bounties":        total_bounties,
        "chart_mining_personal": chart_mining_personal,
        "chart_bounties_dia":    chart_bounties_dia,
        "error_mining":          error_mining,
        "error_bounties":        error_bounties,
    }
    return render(request, "rekstats/mi_dashboard.html", context)
