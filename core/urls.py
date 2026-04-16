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
    path("recent/", views.recent_results, name="recent"),
    path("author/<int:pk>/", views.author_detail, name="author_detail"),
    path("author/<int:pk>/comparisons/", views.author_comparisons, name="author_comparisons"),
    path("work/<int:pk>/", views.work_detail, name="work_detail"),
    path("work/<int:pk>/comparisons/", views.work_comparisons, name="work_comparisons"),
]
