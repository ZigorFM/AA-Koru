from django.urls import path

from . import views

app_name = "rekstats"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("personal/", views.mi_dashboard, name="mi_dashboard"),
]
