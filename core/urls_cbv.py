"""
Modern URL configuration using class-based views.
"""
from django.urls import path
from django.shortcuts import redirect

from .views_cbv import (
    HomeView, CompareView, AuthorLeaderboardView, 
    WorkLeaderboardView, SearchView, AboutView
)

app_name = "core"

def leaderboard_redirect(request):
    """Redirect generic leaderboard to authors leaderboard"""
    return redirect('core:authors_lb')

urlpatterns = [
    path("", HomeView.as_view(), name="home"),
    path("about/", AboutView.as_view(), name="about"),
    path("leaderboard/", leaderboard_redirect, name="leaderboard"),
    path("leaderboard/authors/", AuthorLeaderboardView.as_view(), name="authors_lb"),
    path("leaderboard/works/", WorkLeaderboardView.as_view(), name="works_lb"),
    path("search/", SearchView.as_view(), name="search"),
    path("compare/<str:mode>/", CompareView.as_view(), name="compare"),
]
