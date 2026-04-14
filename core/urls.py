from django.urls import path
from django.shortcuts import redirect
from . import views

app_name = "core"

def leaderboard_redirect(request):
    """Redirect generic leaderboard to authors leaderboard"""
    return redirect('core:authors_lb')

urlpatterns = [
    path("", views.home, name="home"),
    path("about/", views.about, name="about"),
    path("leaderboard/", leaderboard_redirect, name="leaderboard"),
    path("leaderboard/authors/", views.author_leaderboard, name="authors_lb"),
    path("leaderboard/works/", views.work_leaderboard, name="works_lb"),
    path("search/", views.search, name="search"),
    path("compare/<str:mode>/", views.compare, name="compare"),  # mode = "authors" | "works"
    path("recent/", views.recent_results, name="recent"),
]
