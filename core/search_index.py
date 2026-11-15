"""The search index shared by the dev server and the static build.

Search runs entirely in the browser (templates/search.html): the page fetches
this index as ``search-data.json`` and filters it client-side, so the site
needs no database at request time.
"""

from . import rankings


def _rating(obj) -> dict:
    if obj.fit is not None:
        return {"rating": round(obj.fit.rating), "ci": round(obj.fit_ci)}
    return {"rating": round(obj.elo_rating), "ci": None}


def build_search_index() -> dict:
    """Every author and work in leaderboard order, with rank and rounded rating.

    ``rating`` is the fitted rating and ``ci`` its 95% half-width (Elo and
    None before the first fit).

    List order matters: the client shows each match with its leaderboard
    neighbours by slicing around the match's index.
    """
    authors = rankings.fitted_ranking("author")
    works = rankings.fitted_ranking("work")
    return {
        "authors": [
            {"pk": a.pk, "name": a.name, "rank": i, **_rating(a)}
            for i, a in enumerate(authors, start=1)
        ],
        "works": [
            {
                "pk": w.pk,
                "title": w.title,
                "author_name": w.author.name,
                "author_pk": w.author_id,
                "rank": i,
                **_rating(w),
            }
            for i, w in enumerate(works, start=1)
        ],
    }
