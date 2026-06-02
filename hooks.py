from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook

from . import urls


@hooks.register("menu_item_hook")
def register_menu():
    """Entrada principal en el menú lateral de AA."""
    return MenuItemHook(
        "📊 Estadísticas",
        "fas fa-chart-bar fa-fw",
        "rekstats:dashboard",
        navactive=["rekstats:"],
        order=1200,  # >1000 = espacio reservado para community apps
    )


@hooks.register("url_hook")
def register_urls():
    """Registra las URLs de la app bajo /rekstats/."""
    return UrlHook(urls, "rekstats", r"^rekstats/")
