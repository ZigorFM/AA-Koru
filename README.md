# rekstats

App interna de estadísticas para **Rekium Auth** (Alliance Auth v5).

Lee datos directamente de `corptools` y los muestra con gráficas Chart.js
integradas en el menú lateral de AA. Sin Grafana, sin Metabase.

## Vistas

| URL | Descripción |
|-----|-------------|
| `/rekstats/` | Top 10 mineros + Top 10 bounties de la corp (mes actual) |
| `/rekstats/personal/` | Dashboard personal: minería por personaje y bounties diarios |

## Instalación

```bash
source /home/allianceserver/venv/auth/bin/activate
pip install git+https://github.com/TU_USUARIO/rekstats.git
```

Añadir a `local.py`:

```python
INSTALLED_APPS += ['rekstats']
```

Recolectar estáticos y reiniciar:

```bash
cd /home/allianceserver/myauth
python manage.py collectstatic --noinput
sudo supervisorctl restart myauth:
```

## Actualizar

```bash
source /home/allianceserver/venv/auth/bin/activate
pip install --upgrade git+https://github.com/TU_USUARIO/rekstats.git
sudo supervisorctl restart myauth:
```

## Dependencias de corptools

| Tabla | Para qué |
|-------|----------|
| `corptools_characterminingledger` | Top mineros y minería personal |
| `corptools_characterwalletjournalentry` | Top bounties y bounties diarios |
| `corptools_characteraudit` | JOIN intermedio |
| `eveonline_evecharacter` | Nombres y portraits |
| `authentication_characterownership` | Mapeo personaje → usuario |
| `authentication_userprofile` | Resolución de main character |

Los datos se actualizan cada ~30 minutos vía el task
`Character Audit Rolling Update` de corptools.
