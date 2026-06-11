from django.urls import path
from . import views

app_name = "koru_stats"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("personal/", views.mi_dashboard, name="mi_dashboard"),
    path("corp/", views.corp_dashboard, name="corp_dashboard"),
    path("lunas/", views.moon_dashboard_v2, name="moon_dashboard"),
    path("lunas/pagar/<int:payment_id>/", views.moon_mark_paid, name="moon_mark_paid"),
    path("pvp/", views.pvp_dashboard, name="pvp_dashboard"),
    path("auditor/", views.auditor_dashboard, name="auditor_dashboard"),
    path("auditor/piloto/<int:main_id>/", views.auditor_pilot, name="auditor_pilot"),
    path("tickets/", views.tickets_dashboard, name="tickets_dashboard"),
    path("tickets/stats/", views.tickets_stats, name="tickets_stats"),
    path("tickets/<int:ticket_id>/", views.ticket_detail, name="ticket_detail"),
    path("export/resumen/", views.export_csv_summary, name="export_csv_summary"),
    path("export/pvp/",     views.export_csv_pvp,     name="export_csv_pvp"),
    path("export/ore/",     views.export_csv_ore,     name="export_csv_ore"),
]
