from django.urls import path

from . import views

app_name = "website"

urlpatterns = [
    path("", views.search_view, name="search"),
    path("moter/", views.meetings_view, name="meetings"),
    path("moter/<int:pk>/", views.meeting_detail_view, name="meeting-detail"),
    path("rostningar/", views.votes_view, name="votes"),
    path("beslut/", views.decisions_view, name="decisions"),
    path("arende/<int:pk>/", views.agenda_item_view, name="agenda-item"),
    path("ledamot/<str:slug>/", views.member_view, name="member"),
    path("parti/<str:code>/", views.party_view, name="party"),
    path("dokument/<int:pk>/", views.document_detail_view, name="document-detail"),
    path("dokument/version/<int:version_pk>/", views.version_detail_view, name="version-detail"),
    path("dokument/version/<int:version_pk>/fil/", views.version_file_view, name="version-file"),
    path("status/", views.status_view, name="status"),
    path("installningar/", views.settings_view, name="settings"),
]