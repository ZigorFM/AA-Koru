from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook

from . import urls


def _tiene_acceso(request):
    return request.user.has_perm("koru_stats.basic_access")


class RankingMenuHook(MenuItemHook):
    def render(self, request):
        if _tiene_acceso(request):
            return super().render(request)
        return ""


class CorpMenuHook(MenuItemHook):
    def render(self, request):
        if request.user.has_perm("koru_stats.corp_finance_access"):
            return super().render(request)
        return ""


class LunasMenuHook(MenuItemHook):
    def render(self, request):
        if request.user.has_perm("koru_stats.moon_tax_access"):
            return super().render(request)
        return ""


@hooks.register("menu_item_hook")
def register_menu_ranking():
    return RankingMenuHook(
        "🌀 Koru — Estadísticas",
        "fas fa-chart-bar fa-fw",
        "koru_stats:dashboard",
        navactive=["koru_stats:dashboard"],
        order=1200,
    )


@hooks.register("menu_item_hook")
def register_menu_corp():
    return CorpMenuHook(
        "🌀 Koru — Finanzas",
        "fas fa-coins fa-fw",
        "koru_stats:corp_dashboard",
        navactive=["koru_stats:corp_dashboard"],
        order=1201,
    )


@hooks.register("menu_item_hook")
def register_menu_lunas():
    return LunasMenuHook(
        "🌀 Koru — Tax Lunas",
        "fas fa-moon fa-fw",
        "koru_stats:moon_dashboard",
        navactive=["koru_stats:moon_dashboard"],
        order=1202,
    )


class PvpMenuHook(MenuItemHook):
    def render(self, request):
        if _tiene_acceso(request):
            return super().render(request)
        return ""


@hooks.register("menu_item_hook")
def register_menu_pvp():
    return PvpMenuHook(
        "🌀 Koru — PvP",
        "fas fa-crosshairs fa-fw",
        "koru_stats:pvp_dashboard",
        navactive=["koru_stats:pvp_dashboard"],
        order=1203,
    )


class AuditorMenuHook(MenuItemHook):
    def render(self, request):
        if request.user.has_perm("koru_stats.auditor_access"):
            return super().render(request)
        return ""


@hooks.register("menu_item_hook")
def register_menu_auditor():
    return AuditorMenuHook(
        "🌀 Koru — Auditor",
        "fas fa-user-shield fa-fw",
        "koru_stats:auditor_dashboard",
        navactive=["koru_stats:auditor_dashboard"],
        order=1204,
    )


class TicketsMenuHook(MenuItemHook):
    def render(self, request):
        perms = [
            "koru_stats.tickets_admin", "koru_stats.tickets_reclutamiento",
            "koru_stats.tickets_directores", "koru_stats.tickets_asuntos_internos",
            "koru_stats.tickets_it",
        ]
        if any(request.user.has_perm(p) for p in perms):
            return super().render(request)
        return ""


@hooks.register("menu_item_hook")
def register_menu_tickets():
    return TicketsMenuHook(
        "🌀 Koru — Tickets",
        "fas fa-ticket-alt fa-fw",
        "koru_stats:tickets_dashboard",
        navactive=["koru_stats:tickets_dashboard"],
        order=1205,
    )


class DirectoresMenuHook(MenuItemHook):
    def render(self, request):
        if request.user.has_perm("koru_stats.auditor_corp_health"):
            return super().render(request)
        return ""


@hooks.register("menu_item_hook")
def register_menu_corp_health():
    return DirectoresMenuHook(
        "\U0001f300 Koru \u2014 Direcci\u00f3n",
        "fas fa-chart-line fa-fw",
        "koru_stats:corp_health_dashboard",
        navactive=["koru_stats:corp_health_dashboard"],
        order=1206,
    )


@hooks.register("url_hook")
def register_urls():
    return UrlHook(urls, "koru_stats", r"^koru/")
