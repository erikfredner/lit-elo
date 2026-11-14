"""
Build a static HTML version of the site suitable for GitHub Pages.

The static site is a snapshot of the dynamic one: every page is rendered by
requesting its URL through Django's test client and writing the response to
``<output>/<path>/index.html``. Views are the only renderer, so whatever
``runserver`` shows is what gets published. This command only decides *which*
URLs exist.

The output directory is gitignored; publish it to the gh-pages branch with
``ghp-import -n -p _site`` so generated HTML stays out of main's history.

Usage:
    python manage.py build_static              # builds to _site/
    python manage.py build_static -o DIR       # builds to DIR/
"""

import shutil
from math import ceil
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.test import Client, override_settings
from django.urls import reverse

from core import rankings
from core.models import Author, LLMMatchup, Work
from core.views import PAGE_SIZE


def _num_pages(count: int) -> int:
    return max(1, ceil(count / PAGE_SIZE))


def _matchup_counts(content_type: str) -> dict[int, int]:
    """{pk: number of matchups the item appears in}, for items with any."""
    counts: dict[int, int] = {}
    qs = LLMMatchup.objects.filter(content_type=content_type)
    for field in ("item_a_id", "item_b_id"):
        for pk, c in qs.values(field).annotate(c=Count("id")).values_list(field, "c"):
            counts[pk] = counts.get(pk, 0) + c
    return counts


def site_urls() -> list[str]:
    """Every URL path the static site contains."""
    urls = [
        reverse("core:home"),
        reverse("core:about"),
        reverse("core:judges"),
        reverse("core:recent"),
        reverse("core:search"),
        reverse("core:search_data"),
    ]

    def paginated(first_url: str, page_name: str, count: int, **kwargs):
        # Page 1 lives at both the bare URL and …/page/1/, because the
        # pagination links always use the …/page/N/ form.
        urls.append(first_url)
        for n in range(1, _num_pages(count) + 1):
            urls.append(reverse(page_name, kwargs={**kwargs, "page": n}))

    for mode, model in (("authors", Author), ("works", Work)):
        paginated(reverse(f"core:{mode}_lb"), f"core:{mode}_lb_page", model.objects.count())
        content_type = rankings.content_type_for(mode)
        for sc in rankings.available_scopes(content_type):
            count = len(rankings.scoped_ranking(content_type, sc["scope"]))
            if not count:
                continue
            kw = {"scope_key": sc["key"]}
            paginated(
                reverse(f"core:{mode}_lb_scoped", kwargs=kw),
                f"core:{mode}_lb_scoped_page", count, **kw,
            )

    for kind, model in (("author", Author), ("work", Work)):
        counts = _matchup_counts(kind)
        for pk in model.objects.order_by("pk").values_list("pk", flat=True):
            urls.append(reverse(f"core:{kind}_detail", kwargs={"pk": pk}))
            # Detail pages only link to comparison history when there is some.
            if counts.get(pk):
                paginated(
                    reverse(f"core:{kind}_comparisons", kwargs={"pk": pk}),
                    f"core:{kind}_comparisons_page", counts[pk], pk=pk,
                )
    return urls


def output_path(out: Path, url: str) -> Path:
    path = out / url.lstrip("/")
    return path / "index.html" if url.endswith("/") else path


def relativize(html: str, path: Path, out: Path) -> str:
    """Rewrite root-relative links so the site works under any base path
    (GitHub Pages project sites are served from /<repo>/)."""
    prefix = "../" * len(path.parent.relative_to(out).parts)
    return html.replace('href="/', f'href="{prefix}').replace('src="/', f'src="{prefix}')


class Command(BaseCommand):
    help = "Build a static HTML snapshot of the site"

    def add_arguments(self, parser):
        parser.add_argument(
            "-o", "--output",
            default="_site",
            help="Output directory (default: _site)",
        )

    def handle(self, **options):
        out = Path(options["output"])
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)

        static_src = Path("static")
        if static_src.exists():
            shutil.copytree(static_src, out / "static")
        (out / ".nojekyll").touch()  # serve files as-is on GitHub Pages

        urls = site_urls()
        self.stdout.write(f"Rendering {len(urls)} pages...")
        client = Client()
        # Requests never leave the process; allow the test client's host
        # whatever DEBUG/ALLOWED_HOSTS happen to be.
        with override_settings(ALLOWED_HOSTS=["testserver"]):
            self._render(client, urls, out)

        self.stdout.write(self.style.SUCCESS(f"Static site built in {out}/"))

    def _render(self, client, urls, out):
        for url in urls:
            response = client.get(url)
            if response.status_code != 200:
                raise CommandError(f"GET {url} returned {response.status_code}")
            path = output_path(out, url)
            path.parent.mkdir(parents=True, exist_ok=True)
            if response["Content-Type"].startswith("text/html"):
                path.write_text(relativize(response.content.decode(), path, out))
            else:
                path.write_bytes(response.content)
