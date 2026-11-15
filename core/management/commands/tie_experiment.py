"""Side experiment: does a tie option capture real indifference?

Re-asks a stratified sample of already-judged pairings in two arms and
compares them with the original batch judgments:

* ``tie``    — the standard prompt, but the model may answer 0 ("equally canonical").
* ``forced`` — the standard 1-or-2 prompt again (run-to-run baseline for splits).

Nothing here touches ``LLMBatch``, ``LLMMatchup`` or any Elo ladder; state and
raw results live in ``data/experiments/<name>/``.

Usage:
    python manage.py tie_experiment submit [--per-band 80] [--dry-run]
    python manage.py tie_experiment collect [--wait]     # analyzes once every batch is done

    # Non-batch mode (standard price, 2x batch; results in minutes):
    python manage.py tie_experiment submit --sync --name tie-YYYY-MM-DD   # runs and analyzes
    python manage.py tie_experiment collect --sync-pending                # finish pending batches synchronously
"""

from __future__ import annotations

import csv
import json
import random
import re
import time
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core import judging, openrouter
from core.management.commands.submit_llm_batch import DEFAULT_MODELS
from core.models import Author, LLMBatch, LLMMatchup, Work

EXP_ROOT = Path(__file__).resolve().parents[3] / "data" / "experiments"
TIE_INSTRUCTIONS = "Answer 1 or 2, or 0 if they are equally canonical."
TIE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "canonicity_judgment",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"more_canonical": {"type": "integer", "enum": [0, 1, 2]}},
            "required": ["more_canonical"],
            "additionalProperties": False,
        },
    },
}
BANDS = [(1100, "a ≥1100"), (950, "b 950–1100"), (850, "c 850–950"), (float("-inf"), "d <850")]
MODES = {"author": Author, "work": Work}


def band_of(prior: float) -> str:
    return next(label for floor, label in BANDS if prior >= floor)


def parse_answer(content: str | None) -> int | None:
    """Return 0, 1 or 2 from a reply (JSON or bare digit), else None."""
    if not content:
        return None
    text = content.strip()
    for cand in [text] + re.findall(r"\{[^{}]*\}", text):
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            try:
                v = int(obj.get("more_canonical"))
            except (TypeError, ValueError):
                continue
            if v in (0, 1, 2):
                return v
    m = re.fullmatch(r"\W*([012])\W*", text)
    return int(m.group(1)) if m else None


def vote_for_a(order: str, answer: int) -> str:
    """Map an answer to 'A', 'B' or 'tie' given the presentation order."""
    if answer == 0:
        return "tie"
    first_wins = answer == 1
    return "A" if first_wins == (order == "AB") else "B"


def pair_outcome(votes: list[str]) -> str:
    """Classify a pairing's two order-swapped votes."""
    if len(votes) < 2:
        return "missing"
    if votes[0] == votes[1]:
        return "tie" if votes[0] == "tie" else "decisive"
    if "tie" in votes:
        return "lean"
    return "split"


class Command(BaseCommand):
    help = "Side experiment comparing a tie-allowed prompt with the forced 1-or-2 prompt."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["submit", "collect"])
        parser.add_argument("--name", default="tie-2026-11-14")
        parser.add_argument("--per-band", type=int, default=80, help="Pairings per rating band and content type.")
        parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
        parser.add_argument("--seed", type=int, default=11)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--wait", action="store_true")
        parser.add_argument("--poll-interval", type=int, default=60)
        parser.add_argument("--sync", action="store_true", help="submit: run non-batch requests now instead of batches.")
        parser.add_argument(
            "--sync-pending", action="store_true",
            help="collect: run any still-pending batches' requests synchronously (the batches still bill; there is no cancel).",
        )
        parser.add_argument("--concurrency", type=int, default=16, help="Parallel requests in sync mode (default: 16).")

    def handle(self, *args, **opts):
        self.dir = EXP_ROOT / opts["name"]
        self.state_path = self.dir / "state.json"
        if opts["action"] == "submit":
            self._submit(opts)
        else:
            self._collect(opts)

    # ── submit ────────────────────────────────────────────────────────────────

    def _source_batches(self, models: list[str]) -> dict[tuple[str, str], LLMBatch]:
        """The first full-size batch per (model, content type): round 1."""
        out = {}
        for b in LLMBatch.objects.filter(request_count__gt=1000, applied=True).order_by("submitted_at"):
            out.setdefault((b.model_slug, b.content_type), b)
        missing = [(m, ct) for m in models for ct in MODES if (m, ct) not in out]
        if missing:
            raise CommandError(f"No applied source batch for {missing}.")
        return out

    def _submit(self, opts):
        if self.state_path.exists() and not opts["dry_run"]:
            raise CommandError(f"{self.state_path} exists; use another --name.")
        models = [m.strip() for m in opts["models"].split(",") if m.strip()]
        rng = random.Random(opts["seed"])
        sources = self._source_batches(models)

        state = {"models": models, "per_band": opts["per_band"], "sources": {}, "samples": {}, "batches": []}
        for ct, model_cls in MODES.items():
            src = sources[(models[0], ct)]
            pairs = [tuple(p) for p in src.manifest["pairs"]]
            priors = {i.pk: i.mlaib_elo or 0 for i in model_cls.objects.filter(pk__in={pk for p in pairs for pk in p})}
            by_band = defaultdict(list)
            for a, b in pairs:
                by_band[band_of((priors[a] + priors[b]) / 2)].append((a, b))
            sample = []
            for _, label in BANDS:
                group = by_band.get(label, [])
                sample += rng.sample(group, min(opts["per_band"], len(group)))
            state["samples"][ct] = [list(p) for p in sample]
            state["sources"][ct] = {m: sources[(m, ct)].pk for m in models}
            counts = {label: min(opts["per_band"], len(by_band.get(label, []))) for _, label in BANDS}
            self.stdout.write(f"{ct}: {len(sample)} pairings sampled {counts}")

        jobs = self._build_jobs(state)
        for arm, ct, slug, requests in jobs:
            self.stdout.write(f"  {arm:6s} {ct:6s} {slug:30s} {len(requests)} requests")

        if opts["dry_run"]:
            self.stdout.write(json.dumps(jobs[0][3][0], indent=2, ensure_ascii=False))
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        if opts["sync"]:
            for arm, ct, slug, requests in jobs:
                entry = {"arm": arm, "content_type": ct, "model": slug, "id": f"sync-{arm}-{ct}-{slug.replace('/', '_')}"}
                self._run_sync(entry, requests, opts["concurrency"])
                state["batches"].append(entry)
                self.state_path.write_text(json.dumps(state, indent=1))
            self._analyze(state)
            return
        for arm, ct, slug, requests in jobs:
            resp = openrouter.create_batch(slug, requests)
            state["batches"].append({"arm": arm, "content_type": ct, "model": slug, "id": resp["id"], "status": resp.get("status")})
            self.state_path.write_text(json.dumps(state, indent=1))
            self.stdout.write(self.style.SUCCESS(f"  submitted {arm} {ct} {slug}: {resp['id']}"))

    def _build_jobs(self, state) -> list[tuple[str, str, str, list[dict]]]:
        """Request bodies for every (arm, content type, model), rebuilt from ``state``."""
        base_prompt = judging.load_system_prompt()
        arms = {
            "tie": (f"{base_prompt}\n\n{TIE_INSTRUCTIONS}", TIE_FORMAT),
            "forced": (f"{base_prompt}\n\n{judging.OUTPUT_INSTRUCTIONS}", judging.RESPONSE_FORMAT),
        }
        catalog = {m["id"]: m for m in openrouter.list_models()}
        orders = judging.vote_orders(2)
        jobs = []
        for arm, (system_prompt, fmt) in arms.items():
            for ct, model_cls in MODES.items():
                pks = {pk for p in state["samples"][ct] for pk in p}
                qs = model_cls.objects.select_related("author") if ct == "work" else model_cls.objects
                items = {i.pk: i for i in qs.filter(pk__in=pks)}
                mode = "authors" if ct == "author" else "works"
                for slug in state["models"]:
                    params = set((catalog.get(f"{slug}:batch") or {}).get("supported_parameters") or [])
                    requests = []
                    for i, (a, b) in enumerate(state["samples"][ct]):
                        for j, order in enumerate(orders):
                            first, second = (items[a], items[b]) if order == "AB" else (items[b], items[a])
                            body = {
                                "messages": [
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": judging.build_user_prompt(first, second, mode)},
                                ],
                                "max_tokens": 2048,
                            }
                            if "structured_outputs" in params:
                                body["response_format"] = fmt
                            if "reasoning" in params:
                                body["reasoning"] = {"effort": "low"}
                            requests.append({"custom_id": f"p{i}-v{j}", "body": body})
                    jobs.append((arm, ct, slug, requests))
        return jobs

    def _run_sync(self, entry: dict, requests: list[dict], concurrency: int) -> None:
        """Run requests as non-batch calls; save them in the batch results format."""
        started = time.monotonic()
        results = openrouter.run_sync(entry["model"], requests, concurrency)
        cost = openrouter.results_cost(results)
        errors = sum(r["error"] is not None for r in results)
        (self.dir / f"{entry['id']}.json").write_text(json.dumps({"mode": "sync", "results": results, "usage": {"cost": cost}}))
        entry.update(status="completed", cost=cost, mode="sync")
        self.stdout.write(self.style.SUCCESS(
            f"  sync {entry['arm']} {entry['content_type']} {entry['model']}: {len(results)} requests, "
            f"{errors} errors, ${cost:.4f}, {time.monotonic() - started:.0f}s"
        ))

    # ── collect + analyze ─────────────────────────────────────────────────────

    def _collect(self, opts):
        if not self.state_path.exists():
            raise CommandError(f"No experiment at {self.state_path}.")
        state = json.loads(self.state_path.read_text())
        if opts["sync_pending"]:
            jobs = {(arm, ct, slug): reqs for arm, ct, slug, reqs in self._build_jobs(state)}
            for b in state["batches"]:
                if (self.dir / f"{b['id']}.json").exists():
                    continue
                if openrouter.get_batch(b["id"]).get("status") == "completed":
                    continue  # finished meanwhile; collected below at batch price
                self._run_sync(b, jobs[(b["arm"], b["content_type"], b["model"])], opts["concurrency"])
                self.state_path.write_text(json.dumps(state, indent=1))
        while True:
            pending = 0
            for b in state["batches"]:
                raw = self.dir / f"{b['id']}.json"
                if raw.exists():
                    continue
                data = openrouter.get_batch(b["id"])
                b["status"] = data.get("status")
                if b["status"] == "completed" and data.get("results") is not None:
                    raw.write_text(json.dumps(data, ensure_ascii=False))
                    b["cost"] = (data.get("usage") or {}).get("cost")
                elif b["status"] in openrouter.FINAL_STATUSES:
                    raise CommandError(f"Batch {b['id']} ended {b['status']}.")
                else:
                    pending += 1
            self.state_path.write_text(json.dumps(state, indent=1))
            if not pending:
                break
            self.stdout.write(f"{pending} batch(es) still running.")
            if not opts["wait"]:
                return
            self.stdout.flush()
            time.sleep(opts["poll_interval"])
        self._analyze(state)

    def _analyze(self, state):
        orders = judging.vote_orders(2)
        rows = []  # one per (model, content type, pairing)
        for ct, model_cls in MODES.items():
            sample = [tuple(p) for p in state["samples"][ct]]
            priors = {i.pk: i.mlaib_elo or 0 for i in model_cls.objects.filter(pk__in={pk for p in sample for pk in p})}
            for slug in state["models"]:
                original = {
                    (m.item_a_id, m.item_b_id): m.score_a
                    for m in LLMMatchup.objects.filter(batch_id=state["sources"][ct][slug])
                }
                arm_votes = {}
                for b in state["batches"]:
                    if b["content_type"] != ct or b["model"] != slug:
                        continue
                    data = json.loads((self.dir / f"{b['id']}.json").read_text())
                    votes = defaultdict(dict)
                    for r in data.get("results") or []:
                        pi, vi = (int(x[1:]) for x in r["custom_id"].split("-"))
                        body = ((r.get("response") or {}).get("body")) or {}
                        content = (((body.get("choices") or [{}])[0]).get("message") or {}).get("content")
                        ans = parse_answer(content)
                        if ans is None or (b["arm"] == "forced" and ans == 0):
                            continue
                        votes[pi][vi] = vote_for_a(orders[vi], ans)
                    arm_votes[b["arm"]] = votes
                for pi, (a, bb) in enumerate(sample):
                    s = original.get((a, bb))
                    orig = None if s is None else ("split" if s == 0.5 else "decisive")
                    orig_dir = None if s in (None, 0.5) else ("A" if s > 0.5 else "B")
                    tv = [arm_votes["tie"][pi][v] for v in sorted(arm_votes["tie"].get(pi, {}))]
                    fv = [arm_votes["forced"][pi][v] for v in sorted(arm_votes["forced"].get(pi, {}))]
                    rows.append({
                        "content_type": ct, "model": slug, "band": band_of((priors[a] + priors[bb]) / 2),
                        "item_a": a, "item_b": bb, "original": orig, "original_dir": orig_dir,
                        "retest": pair_outcome(fv), "retest_dir": fv[0] if pair_outcome(fv) == "decisive" else None,
                        "tie_arm": pair_outcome(tv), "tie_votes": sum(v == "tie" for v in tv), "tie_n": len(tv),
                        "tie_dir": tv[0] if pair_outcome(tv) == "decisive" else None,
                    })

        with (self.dir / "pairs.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

        def pct(n, d):
            return f"{100 * n / d:5.1f}%" if d else "    –"

        out = []
        out.append("Per model and band (pairings; % of pairings):")
        out.append(f"{'model':28s} {'band':12s} {'n':>4s}  {'orig split':>10s} {'retest split':>12s} │ {'tie vote':>8s} {'both tie':>8s} {'lean':>6s} {'split':>6s}")
        groups = defaultdict(list)
        for r in rows:
            groups[(r["model"], r["band"])].append(r)
        for (model, band), g in sorted(groups.items()):
            n = len(g)
            tv = sum(r["tie_votes"] for r in g)
            tn = sum(r["tie_n"] for r in g)
            out.append(
                f"{model.split('/')[-1]:28s} {band:12s} {n:4d}  {pct(sum(r['original'] == 'split' for r in g), n):>10s} "
                f"{pct(sum(r['retest'] == 'split' for r in g), n):>12s} │ {pct(tv, tn):>8s} "
                f"{pct(sum(r['tie_arm'] == 'tie' for r in g), n):>8s} {pct(sum(r['tie_arm'] == 'lean' for r in g), n):>6s} "
                f"{pct(sum(r['tie_arm'] == 'split' for r in g), n):>6s}"
            )

        out.append("\nDoes a tie vote track measured indifference? Tie-vote rate by the pairing's original outcome:")
        for model in state["models"]:
            g = [r for r in rows if r["model"] == model]
            parts = []
            for orig in ("split", "decisive"):
                sub = [r for r in g if r["original"] == orig]
                parts.append(f"originally {orig}: {pct(sum(r['tie_votes'] for r in sub), sum(r['tie_n'] for r in sub))} (n={len(sub)})")
            out.append(f"  {model.split('/')[-1]:24s} " + " · ".join(parts))

        out.append("\nRun-to-run baseline: forced retest of originally split pairings:")
        for model in state["models"]:
            sub = [r for r in rows if r["model"] == model and r["original"] == "split"]
            out.append(f"  {model.split('/')[-1]:24s} still split {pct(sum(r['retest'] == 'split' for r in sub), len(sub))} (n={len(sub)})")

        out.append("\nDirection agreement with the original verdict (originally decisive pairings):")
        for model in state["models"]:
            g = [r for r in rows if r["model"] == model and r["original"] == "decisive"]
            rt = [r for r in g if r["retest_dir"]]
            td = [r for r in g if r["tie_dir"]]
            lost = sum(r["tie_arm"] in ("tie", "lean") for r in g)
            out.append(
                f"  {model.split('/')[-1]:24s} forced retest {pct(sum(r['retest_dir'] == r['original_dir'] for r in rt), len(rt))} "
                f"· tie arm (decisive only) {pct(sum(r['tie_dir'] == r['original_dir'] for r in td), len(td))} "
                f"· turned into tie/lean {pct(lost, len(g))}"
            )

        out.append("\nCross-model: both-tie agreement on the same pairing:")
        by_pair = defaultdict(dict)
        for r in rows:
            by_pair[(r["content_type"], r["item_a"], r["item_b"])][r["model"]] = r["tie_arm"] == "tie"
        both = [v for v in by_pair.values() if len(v) == 2]
        m1, m2 = state["models"][:2]
        out.append(
            f"  ties by {m1.split('/')[-1]}: {sum(v[m1] for v in both)}, by {m2.split('/')[-1]}: {sum(v[m2] for v in both)}, "
            f"by both: {sum(v[m1] and v[m2] for v in both)} of {len(both)} pairings"
        )
        cost = sum(b.get("cost") or 0 for b in state["batches"])
        out.append(f"\nCost: ${cost:.4f}. Per-pairing rows: {self.dir / 'pairs.csv'}")
        report = "\n".join(out)
        (self.dir / "report.txt").write_text(report)
        self.stdout.write(report)
