"""
Migración de rendimiento — índices en tablas de corptools y moons.

Estos índices NO son de nuestros modelos, pero Django permite crearlos
con RunSQL. Se usan IF NOT EXISTS para que sea seguro re-ejecutar.

Tablas afectadas (externas, sólo añadimos índices):
  - corptools_characterminingledger
  - corptools_characterwalletjournalentry
  - corptools_corporationwalletjournalentry
  - moons_miningobservation

NOTA: Si corptools ya tiene alguno de estos índices en tu instalación
la sentencia IF NOT EXISTS simplemente no hace nada. Sin riesgo.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("koru_stats", "0003_alter_general_options_alter_moontaxconfig_tag_and_more"),
    ]

    operations = [
        # ── corptools_characterminingledger ───────────────────────────────
        # Filtro principal: WHERE date >= %s AND date < %s
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS idx_koru_cml_date
                ON corptools_characterminingledger (date);
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS idx_koru_cml_date
                ON corptools_characterminingledger;
            """,
        ),
        # JOIN: ON cau.id = ml.character_id
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS idx_koru_cml_character_id
                ON corptools_characterminingledger (character_id);
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS idx_koru_cml_character_id
                ON corptools_characterminingledger;
            """,
        ),

        # ── corptools_characterwalletjournalentry ─────────────────────────
        # Filtro compuesto: date + ref_type (bounty_prizes, ess_escrow_transfer…)
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS idx_koru_cwje_date_ref
                ON corptools_characterwalletjournalentry (date, ref_type);
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS idx_koru_cwje_date_ref
                ON corptools_characterwalletjournalentry;
            """,
        ),
        # JOIN: ON cau.id = wj.character_id
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS idx_koru_cwje_character_id
                ON corptools_characterwalletjournalentry (character_id);
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS idx_koru_cwje_character_id
                ON corptools_characterwalletjournalentry;
            """,
        ),

        # ── corptools_corporationwalletjournalentry ───────────────────────
        # Filtro compuesto: date + ref_type para corp_dashboard y tendencias
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS idx_koru_corpwje_date_ref
                ON corptools_corporationwalletjournalentry (date, ref_type);
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS idx_koru_corpwje_date_ref
                ON corptools_corporationwalletjournalentry;
            """,
        ),
        # context_id se usa en JOIN con mapsystem para top_sistemas
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS idx_koru_corpwje_context_id
                ON corptools_corporationwalletjournalentry (context_id);
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS idx_koru_corpwje_context_id
                ON corptools_corporationwalletjournalentry;
            """,
        ),

        # ── moons_miningobservation ───────────────────────────────────────
        # Filtro: WHERE last_updated >= %s AND last_updated < %s
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS idx_koru_moonobs_updated
                ON moons_miningobservation (last_updated);
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS idx_koru_moonobs_updated
                ON moons_miningobservation;
            """,
        ),

        # ── authentication_characterownership ────────────────────────────
        # JOIN: ON co.character_id = ec.id  (aparece en TODOS los queries)
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS idx_koru_co_character_id
                ON authentication_characterownership (character_id);
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS idx_koru_co_character_id
                ON authentication_characterownership;
            """,
        ),

        # ── authentication_userprofile ────────────────────────────────────
        # JOIN/filtro: ON up.user_id = co.user_id  y  WHERE up.main_character_id = %s
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS idx_koru_up_main_char
                ON authentication_userprofile (main_character_id);
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS idx_koru_up_main_char
                ON authentication_userprofile;
            """,
        ),
    ]
