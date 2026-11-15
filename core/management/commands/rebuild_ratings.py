"""Recompute aggregate and per-model/per-maker Elo ratings from LLMMatchup history.

Resets every item to its prior (``mlaib_elo`` or the default) and replays all
matchups in order. Use after deleting matchups or changing the Elo constants.

Usage:
    python manage.py rebuild_ratings [--mode authors|works|both]
"""

from django.core.management.base import BaseCommand

from core.ratings import rebuild_ratings


class Command(BaseCommand):
    help = "Rebuild all Elo ladders by replaying LLMMatchup history."

    def add_arguments(self, parser):
        parser.add_argument("--mode", choices=["authors", "works", "both"], default="both")

    def handle(self, *args, **opts):
        modes = ["author", "work"] if opts["mode"] == "both" else [opts["mode"].rstrip("s")]
        for ct in modes:
            n = rebuild_ratings(ct)
            self.stdout.write(self.style.SUCCESS(f"Replayed {n} {ct} matchups."))
        from core import bt

        bt.refit_all()
        self.stdout.write("Refit Bradley–Terry ratings.")
