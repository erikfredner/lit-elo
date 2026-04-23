"""
Build a static HTML version of the site suitable for GitHub Pages.

Usage:
    python manage.py build_static              # builds to docs/
    python manage.py build_static -o _site     # builds to _site/
"""

import json
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.template.loader import render_to_string

from core.constants import DEFAULT_ELO_RATING
from core.models import Author, Work, LLMMatchup
from core.views import _pagination_items


class Command(BaseCommand):
    help = "Build a static HTML snapshot of the site"

    def add_arguments(self, parser):
        parser.add_argument(
            "-o", "--output",
            default="docs",
            help="Output directory (default: docs)",
        )

    def handle(self, **options):
        out = Path(options["output"])
        self.out = out
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)

        # Copy static assets
        static_src = Path("static")
        if static_src.exists():
            shutil.copytree(static_src, out / "static")

        # GitHub Pages marker
        (out / ".nojekyll").touch()

        # ── Precompute data ──────────────────────────────────────────────
        self.stdout.write("Loading data...")
        all_authors = list(Author.objects.order_by("-elo_rating", "name"))
        all_works = list(
            Work.objects.select_related("author").order_by("-elo_rating", "title")
        )
        author_ranks = {a.pk: i + 1 for i, a in enumerate(all_authors)}
        work_ranks = {w.pk: i + 1 for i, w in enumerate(all_works)}

        author_comp_counts = self._all_matchup_counts("author")
        work_comp_counts = self._all_matchup_counts("work")

        self.stdout.write("Loading Elo histories...")
        author_histories = self._all_elo_histories("author")
        work_histories = self._all_elo_histories("work")

        ctx_base = {}

        # ── Home page ────────────────────────────────────────────────────
        self.stdout.write("Rendering home page...")
        author_series = [
            {"name": a.name, "pk": a.pk, "history": author_histories.get(a.pk, [DEFAULT_ELO_RATING])}
            for a in all_authors[:10]
        ]
        work_series = [
            {"name": w.title, "pk": w.pk, "history": work_histories.get(w.pk, [DEFAULT_ELO_RATING])}
            for w in all_works[:10]
        ]
        self._render_page(out / "index.html", "home.html", {
            **ctx_base,
            "author_series": author_series,
            "work_series": work_series,
            "author_series_json": json.dumps([{"name": s["name"], "history": s["history"]} for s in author_series]),
            "work_series_json": json.dumps([{"name": s["name"], "history": s["history"]} for s in work_series]),
            "current_page": "home",
        })

        # ── About ────────────────────────────────────────────────────────
        self.stdout.write("Rendering about...")
        self._render_page(out / "about/index.html", "about.html", {
            **ctx_base, "current_page": "about",
        })

        # ── Recent ───────────────────────────────────────────────────────
        self.stdout.write("Rendering recent...")
        self._render_recent(out, all_authors, all_works, ctx_base)

        # ── Leaderboards ─────────────────────────────────────────────────
        self.stdout.write("Rendering author leaderboard...")
        self._render_leaderboard(
            out, all_authors, "authors", "Authors by Canonicity",
            author_comp_counts, ctx_base,
        )
        self.stdout.write("Rendering work leaderboard...")
        self._render_leaderboard(
            out, all_works, "works", "Works by Canonicity",
            work_comp_counts, ctx_base,
        )

        # ── Detail pages ─────────────────────────────────────────────────
        self.stdout.write(f"Rendering {len(all_authors)} author detail pages...")
        for author in all_authors:
            author_works = [w for w in all_works if w.author_id == author.pk]
            works_with_rank = [
                {"work": w, "rank": work_ranks[w.pk]} for w in author_works
            ]
            history = author_histories.get(author.pk, [])
            elo_history_json = json.dumps(history) if len(history) > 1 else ""
            self._render_page(
                out / f"author/{author.pk}/index.html",
                "author_detail.html",
                {
                    **ctx_base,
                    "author": author,
                    "rank": author_ranks[author.pk],
                    "works_with_rank": works_with_rank,
                    "elo_history_json": elo_history_json,
                },
            )

        self.stdout.write(f"Rendering {len(all_works)} work detail pages...")
        for work in all_works:
            author_works = [w for w in all_works if w.author_id == work.author_id]
            works_with_rank = [
                {
                    "work": w,
                    "rank": work_ranks[w.pk],
                    "is_current": w.pk == work.pk,
                }
                for w in author_works
            ]
            history = work_histories.get(work.pk, [])
            elo_history_json = json.dumps(history) if len(history) > 1 else ""
            self._render_page(
                out / f"work/{work.pk}/index.html",
                "work_detail.html",
                {
                    **ctx_base,
                    "work": work,
                    "rank": work_ranks[work.pk],
                    "works_with_rank": works_with_rank,
                    "elo_history_json": elo_history_json,
                },
            )

        # ── Comparison history pages ──────────────────────────────────────
        self.stdout.write("Rendering author comparison histories...")
        author_lookup = {a.pk: a for a in all_authors}
        for author in all_authors:
            self._render_comparisons(
                out, author.pk, author.name,
                f"/author/{author.pk}/",
                "author", author_lookup, ctx_base,
            )

        self.stdout.write("Rendering work comparison histories...")
        work_lookup = {w.pk: w for w in all_works}
        for work in all_works:
            self._render_comparisons(
                out, work.pk, work.title,
                f"/work/{work.pk}/",
                "work", work_lookup, ctx_base,
            )

        # ── Search ────────────────────────────────────────────────────────
        self.stdout.write("Rendering search...")
        self._render_search(out, all_authors, all_works, author_ranks, work_ranks, ctx_base)

        self.stdout.write(self.style.SUCCESS(f"Static site built in {out}/"))

    # ── Helpers ──────────────────────────────────────────────────────────

    def _write(self, path: Path, content: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def _relativize(self, html: str, path: Path) -> str:
        depth = len(path.parent.relative_to(self.out).parts)
        prefix = "../" * depth
        html = html.replace('href="/', f'href="{prefix}')
        html = html.replace('src="/', f'src="{prefix}')
        html = html.replace("fetch('/search-data.json')", f"fetch('{prefix}search-data.json')")
        return html

    def _render_page(self, path: Path, template: str, context: dict):
        html = render_to_string(template, context)
        html = self._relativize(html, path)
        self._write(path, html)

    def _all_matchup_counts(self, content_type: str) -> dict:
        a = dict(
            LLMMatchup.objects.filter(content_type=content_type)
            .values("item_a_id")
            .annotate(c=Count("id"))
            .values_list("item_a_id", "c")
        )
        b = dict(
            LLMMatchup.objects.filter(content_type=content_type)
            .values("item_b_id")
            .annotate(c=Count("id"))
            .values_list("item_b_id", "c")
        )
        all_pks = set(a) | set(b)
        return {pk: a.get(pk, 0) + b.get(pk, 0) for pk in all_pks}

    def _all_elo_histories(self, content_type: str) -> dict:
        """Return {pk: [starting_elo, elo_after_match_1, ...]} in match order.
        Entities with no matches are absent from the dict."""
        matchups = (
            LLMMatchup.objects
            .filter(content_type=content_type)
            .order_by("created_at")
            .values("item_a_id", "item_b_id", "elo_a_after", "elo_b_after")
        )
        histories = {}
        for m in matchups:
            for pk, elo in ((m["item_a_id"], m["elo_a_after"]), (m["item_b_id"], m["elo_b_after"])):
                if pk not in histories:
                    histories[pk] = [DEFAULT_ELO_RATING]
                histories[pk].append(elo)
        return histories

    def _render_leaderboard(self, out, items, mode, title, comp_counts, ctx_base):
        content_type = "author" if mode == "authors" else "work"
        total_comparisons = LLMMatchup.objects.filter(content_type=content_type).count()

        paginator = Paginator(items, 50)
        base_dir = out / f"leaderboard/{mode}"

        for page_num in range(1, paginator.num_pages + 1):
            page_obj = paginator.page(page_num)
            for obj in page_obj:
                obj.comp_count = comp_counts.get(obj.pk, 0)

            page_items = _pagination_items(page_num, paginator.num_pages)

            page_url_prefix = f"/leaderboard/{mode}/page/"
            context = {
                **ctx_base,
                "page_obj": page_obj,
                "title": title,
                "mode": mode,
                "page_items": page_items,
                "total_comparisons": total_comparisons,
                "current_page": f"{mode}_lb",
                "page_url_prefix": page_url_prefix,
            }
            html = render_to_string("leaderboard.html", context)

            if page_num == 1:
                self._write(base_dir / "index.html", self._relativize(html, base_dir / "index.html"))
            page_path = base_dir / f"page/{page_num}/index.html"
            self._write(page_path, self._relativize(html, page_path))

    def _render_recent(self, out, all_authors, all_works, ctx_base):
        author_matchups = list(
            LLMMatchup.objects.filter(content_type="author").order_by("-created_at")[:10]
        )
        work_matchups = list(
            LLMMatchup.objects.filter(content_type="work").order_by("-created_at")[:10]
        )

        authors_by_id = {a.pk: a for a in all_authors}
        works_by_id = {w.pk: w for w in all_works}

        def build_rows(matchups, lookup):
            rows = []
            for m in matchups:
                item_a = lookup.get(m.item_a_id)
                item_b = lookup.get(m.item_b_id)
                if not item_a or not item_b:
                    continue
                if m.winner == "A":
                    winner, loser = item_a, item_b
                    delta = m.elo_a_after - m.elo_a_before
                else:
                    winner, loser = item_b, item_a
                    delta = m.elo_b_after - m.elo_b_before
                rows.append({
                    "winner": winner,
                    "loser": loser,
                    "delta": delta,
                    "created_at": m.created_at,
                    "model_used": m.model_used,
                })
            return rows

        self._render_page(out / "recent/index.html", "recent.html", {
            **ctx_base,
            "author_rows": build_rows(author_matchups, authors_by_id),
            "work_rows": build_rows(work_matchups, works_by_id),
            "current_page": "recent",
        })

    def _render_comparisons(self, out, pk, item_name, detail_url, content_type, lookup, ctx_base):
        matchups_qs = LLMMatchup.objects.filter(
            Q(content_type=content_type, item_a_id=pk)
            | Q(content_type=content_type, item_b_id=pk)
        ).order_by("-created_at")

        total_comparisons = matchups_qs.count()
        if total_comparisons == 0:
            return

        paginator = Paginator(matchups_qs, 50)
        entity = "author" if content_type == "author" else "work"
        base_dir = out / f"{entity}/{pk}/comparisons"

        for page_num in range(1, paginator.num_pages + 1):
            page_obj = paginator.page(page_num)
            rows = self._build_comparison_rows(page_obj, pk, content_type, lookup)
            page_items = _pagination_items(page_num, paginator.num_pages)

            page_url_prefix = f"/{entity}/{pk}/comparisons/page/"
            context = {
                **ctx_base,
                "item_name": item_name,
                "detail_url": detail_url,
                "rows": rows,
                "page_obj": page_obj,
                "page_items": page_items,
                "total_comparisons": total_comparisons,
                "page_url_prefix": page_url_prefix,
            }
            html = render_to_string("item_comparisons.html", context)

            if page_num == 1:
                self._write(base_dir / "index.html", self._relativize(html, base_dir / "index.html"))
            page_path = base_dir / f"page/{page_num}/index.html"
            self._write(page_path, self._relativize(html, page_path))

    def _build_comparison_rows(self, matchups_page, pk, content_type, lookup):
        entity = "author" if content_type == "author" else "work"
        rows = []
        for m in matchups_page:
            is_a = m.item_a_id == pk
            opp_pk = m.item_b_id if is_a else m.item_a_id
            opponent = lookup.get(opp_pk)
            if opponent is None:
                continue
            won = (m.winner == "A") == is_a
            elo_before = m.elo_a_before if is_a else m.elo_b_before
            elo_after = m.elo_a_after if is_a else m.elo_b_after
            rows.append({
                "opponent": opponent,
                "opponent_url": f"/{entity}/{opp_pk}/",
                "won": won,
                "elo_before": elo_before,
                "elo_after": elo_after,
                "delta": elo_after - elo_before,
                "created_at": m.created_at,
                "model_used": m.model_used,
            })
        return rows

    def _render_search(self, out, all_authors, all_works, author_ranks, work_ranks, ctx_base):
        # Emit search data JSON
        authors_data = [
            {
                "pk": a.pk,
                "name": a.name,
                "rank": author_ranks[a.pk],
                "elo": round(a.elo_rating),
            }
            for a in all_authors
        ]
        works_data = [
            {
                "pk": w.pk,
                "title": w.title,
                "author_name": w.author.name,
                "author_pk": w.author_id,
                "rank": work_ranks[w.pk],
                "elo": round(w.elo_rating),
            }
            for w in all_works
        ]
        search_data = {"authors": authors_data, "works": works_data}
        self._write(
            out / "search-data.json",
            json.dumps(search_data, ensure_ascii=False),
        )

        # Render the search page with inline JS
        self._render_page(out / "search/index.html", "search_static.html", {
            **ctx_base,
            "current_page": "search",
        })
