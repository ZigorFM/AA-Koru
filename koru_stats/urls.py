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
]
