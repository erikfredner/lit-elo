"""The search index shared by the dev server and the static build.

Search runs entirely in the browser (templates/search.html): the page fetches
this index as ``search-data.json`` and filters it client-side, so the site
needs no database at request time.
"""

from .models import Author, Work


def build_search_index() -> dict:
    """Every author and work in leaderboard order, with rank and rounded Elo.

    List order matters: the client shows each match with its leaderboard
    neighbours by slicing around the match's index.
    """
    authors = Author.objects.order_by("-elo_rating", "name")
    works = Work.objects.select_related("author").order_by("-elo_rating", "title")
    return {
        "authors": [
            {"pk": a.pk, "name": a.name, "rank": i, "elo": round(a.elo_rating)}
            for i, a in enumerate(authors, start=1)
        ],
        "works": [
            {
                "pk": w.pk,
                "title": w.title,
                "author_name": w.author.name,
                "author_pk": w.author_id,
                "rank": i,
                "elo": round(w.elo_rating),
            }
            for i, w in enumerate(works, start=1)
        ],
    }
