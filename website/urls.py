from django.urls import path

from . import views

app_name = "website"

urlpatterns = [
    path("", views.search_view, name="search"),
    path("moter/", views.meetings_view, name="meetings"),
    path("moter/<int:pk>/", views.meeting_detail_view, name="meeting-detail"),
    path("dokument/<int:pk>/", views.document_detail_view, name="document-detail"),
    path("dokument/version/<int:version_pk>/", views.version_detail_view, name="version-detail"),
    path("dokument/version/<int:version_pk>/fil/", views.version_file_view, name="version-file"),
    path("status/", views.status_view, name="status"),
    path("installningar/", views.settings_view, name="settings"),
]