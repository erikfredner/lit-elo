"""Refit Bradley–Terry ratings (with uncertainty) for every scope.

The site's leaderboards and detail pages show these fitted ratings (with the
MLAIB prior; see ``core.bt``); the Elo ladders remain for the history charts. ``collect_llm_batches``,
``submit_llm_batch --sync`` and ``build_static`` refit automatically.

Usage:
    python manage.py fit_ratings
"""

import time

from django.core.management.base import BaseCommand

from core import bt


class Command(BaseCommand):
    help = "Refit Bradley–Terry ratings and rank intervals for every scope."

    def handle(self, *args, **opts):
        started = time.monotonic()
        for ct, scopes in bt.refit_all().items():
            for scope, n in scopes.items():
                self.stdout.write(f"  {ct:6} {scope:40} {n} items")
        self.stdout.write(self.style.SUCCESS(f"Refit in {time.monotonic() - started:.0f}s."))
