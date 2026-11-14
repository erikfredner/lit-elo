# Canon Wars: Literary Canonicity Rankings

A literary canonicity ranking system that uses ELO ratings to rank authors and works through LLM-judged head-to-head comparisons. Rankings are generated locally using an offline data pipeline and published as a static site on GitHub Pages.

## How It Works

### ELO Algorithm

- Standard ELO with K-factor of 32 and default starting rating of 1200
- New items are seeded with an informed prior (`mlaib_elo`) derived from MLAIB citation-count z-scores (range 800–1600) before matchups run

### Batch LLM Matchups (OpenRouter)

The main pipeline uses the [OpenRouter Batch API](https://openrouter.ai/docs/batch-quickstart). It costs about half the sync price, and batches finish within 24 hours (Google can take up to 48–96h). There are two steps:

```bash
python manage.py submit_llm_batch --mode both --count 50            # one batch per model × mode
python manage.py submit_llm_batch --mode works --count 200 --models openai/gpt-6-luna,z-ai/glm-5.3-flash
python manage.py submit_llm_batch --mode authors --count 5 --dry-run  # cost estimate + sample request body
python manage.py collect_llm_batches --wait                          # poll, then apply finished batches
python manage.py rebuild_ratings                                     # replay all matchups from priors
```

- **Judges.** The default models are `anthropic/claude-haiku-5.5` and `openai/gpt-6-luna` (GLM-5.3 Flash, DeepSeek V4.1 Flash and MiniMax M3 were in the first test batches and can be added with `--models`). Pass base slugs; OpenRouter resolves the `:batch` variant itself. Every model judges the *same* pairings (unless `--separate-pairings`), so their verdicts can be compared directly.
- **Binary votes.** The prompt ends "Answer 1 or 2." and each vote is just `{"more_canonical": 1|2}` (or a bare `1`/`2`). Each pairing gets `--votes-per-pairing` votes (default 2), alternating the presentation order (AB, BA) to cancel position bias. The matchup's `score_a` is the share of votes A won (1, 0.5 or 0 with two votes), so probabilities come from the rating fit across many games rather than from self-reported confidence, which isn't comparable across models. As of November 2026, no batch endpoint for these models returns logprobs (GLM accepts the parameter but returns `null`). Votes with a stated confidence or logprobs (the first test batches) are still scored as clamped probabilities.
- **Structured output.** Requests use a strict one-field `json_schema` where the model supports it. Otherwise the model answers in plain text (MiniMax). The parser accepts a bare `1`/`2` or JSON wrapped in prose or code fences.
- **Ladders.** Each matchup updates the aggregate `elo_rating`, an `ItemRating` row for `model:<slug>`, and one for `maker:<maker>`. Every ladder starts from the item's `mlaib_elo` prior.
- **Bookkeeping.** `LLMBatch` records each submission: its OpenRouter id, the pairing manifest, status and actual cost. Raw results are saved to `data/batches/<id>.json` because OpenRouter deletes them after 30 days. `--max-cost` (default $2) blocks a submission whose estimate is too high.

The site shows per-maker and per-model leaderboards (`/leaderboard/authors/by/model-openai--gpt-6-luna/`) and a per-model table on each detail page. The `/judges/` page reports each model's decisiveness, order consistency, position bias, and agreement with the other models.

### Legacy sync matchups (OpenAI)

The `run_llm_elo` management command pairs authors (or works), asks an LLM to judge which is more canonical, and writes results directly to the database. Each judgment is persisted as an `LLMMatchup` record with before/after ELO values and the model used.

**Pairing algorithm** — used by both `run_llm_elo` and `submit_llm_batch`. The first item of each pair is always one of the least-played items so far (history plus the batch being built, ties at random). Its opponent is chosen to maximize information gain using three factors:

1. *ELO proximity* — pairs with similar ratings are preferred, since near-equal matchups keep the win probability close to 50% and thus maximize binary entropy per comparison: `exp(-elo_diff / 200)`.
2. *Item novelty* — under-compared items are preferred via `1 / sqrt(games_played + 1)`, directing the budget toward unexplored items before revisiting settled ones.
3. *Pair novelty* — previously judged pairs are downweighted via `1 / sqrt(pair_count + 1)`, so historical matchups are soft-penalized rather than hard-blocked.

The combined weight for each candidate opponent is `elo_proximity × novelty_b × pair_novelty`. Within a single batch run, each pair appears at most once. Games are counted per pairing (the max over judge models), so a pairing judged by two models counts once.

Picking the first item least-played-first keeps total games even across the ladder. When it was sampled at random (before round 2 of Scenario A, November 2026), items rated far above the pack, which is the top of the canon, were rarely chosen as anyone's opponent: in round 1, authors with a prior of 1200 or more averaged 2.9 games against 5.1 for the rest, and some had none. The new rule makes up that deficit in the following rounds.

```bash
python manage.py run_llm_elo --mode authors --count 50
python manage.py run_llm_elo --mode works   --count 50
python manage.py run_llm_elo --mode authors --count 10 --dry-run  # preview prompts, no API calls
```

Default model: `gpt-5.4-nano`. Override with `--model <id>`. Requires `OPENAI_API_KEY` in the environment (or `.env` file).

### Self-Consistency Testing

`test_self_consistency` runs the same pairings multiple times and reports how often the model agrees with itself — an empirical measure of judgment reliability before trusting ELO ratings.

```bash
python manage.py test_self_consistency                          # 1000 pairs × both modes × 5 reps (10000 calls)
python manage.py test_self_consistency --mode authors           # authors only
python manage.py test_self_consistency --count 100 --reps 3    # smaller run
python manage.py test_self_consistency --dry-run --count 10    # preview pairings, no API calls
```

Pairings are generated with a fixed `--seed` (default 42) so results are reproducible across runs. The command reports a consistency distribution (5/5, 4/5, 3/5 agreement), overall self-consistency rate, and sample tables of perfectly consistent and least consistent pairings — for authors and works separately. Does not modify the database.

### Convergence Analysis

`analyze_convergence` answers whether running more `run_llm_elo` batches is still informative or whether observed ELO changes are just noise from the model's ~13% self-inconsistency floor.

```bash
python manage.py analyze_convergence                             # both modes, auto-detect batches
python manage.py analyze_convergence --mode authors              # authors only
python manage.py analyze_convergence --batch-size 200            # group every 200 matchups
python manage.py analyze_convergence --output-csv /tmp/conv.csv  # export for plotting
```

Groups historical `LLMMatchup` records into batches (auto-detected by time gap between runs, or fixed `--batch-size`) and reports per batch:
- **Upset rate**: fraction of matchups where the lower-ELO item won — should converge toward the self-inconsistency noise floor (~13%) as ratings stabilize
- **Mean |ΔELO|**: average absolute ELO change per match — decreases as matchups become more predictable

When the upset rate stops declining and hovers near the noise floor, further runs are mostly noise. Does not modify the database.

### Static Site

`python manage.py build_static` renders the entire site to `_site/` (gitignored) as flat HTML files:

- Leaderboards (paginated, one HTML file per page)
- Author and work detail pages
- Comparison history pages
- Recent results
- Client-side search (accent-insensitive, with rank context display)

Each page is produced by requesting its URL from the Django views and saving the response, so `runserver` shows exactly what gets published, search included (both use the same `search-data.json` index).

The live site has no server — all pages are pre-rendered from the local Django/SQLite database. Rankings update when the pipeline runs locally and the site is rebuilt and redeployed.

## Quick Start

Requires Python 3.13+ and Django 5.2+.

```bash
git clone <repository-url>
cd lit-elo
uv sync
python manage.py migrate
python manage.py loaddata fixtures/authors.json fixtures/works.json
python manage.py runserver
```

## Updating and Deploying Rankings

```bash
# Run LLM matchups to update ELO ratings
python manage.py run_llm_elo --mode authors --count 200
python manage.py run_llm_elo --mode works   --count 200

# Build the static site into _site/
python manage.py build_static

# Preview locally (or just use runserver, which renders the same pages)
python -m http.server -d _site

# Deploy: commit _site/ to the gh-pages branch and push it
ghp-import -n -p _site
```

## Database Models

- **Author** — name, birth/death years, `elo_rating` (live matchup rating), `mlaib_elo` (citation-count prior), `viaf_id`
- **Work** — title, author (FK), publication year, form, `elo_rating`, `mlaib_elo`
- **LLMMatchup** — one record per LLM judgment: content type, item A/B PKs, winner, ELO before/after, model used, timestamp

## Running Tests

```bash
pytest                    # all tests (Django tests run against a temporary test DB)
pytest tests/test_elo.py  # single file
```

## Data Pipeline Scripts

Scripts in `scripts/` process the raw MLAIB bibliography data in `data/` (gitignored).

| Script | Purpose |
|--------|---------|
| `build_author_work_mapping.py` | Parse MLAIB XML in `data/mlaib_data/` into `authors.csv` / `works.csv` (plus mapping/presence CSVs). Independent of Django. |
| `normalize_mlaib.py` | Parsing/normalization helpers used by the script above; also runnable on its own. |
| `lookup_viaf.py` | Add VIAF IDs to `data/authors.csv` via the VIAF AutoSuggest API (resumable cache). Independent of Django. |
| `lookup_wikipedia.py` | Uses Django: fill `Author.wikipedia_url` from VIAF links for authors with a `viaf_id`. |

Typical pipeline:

```bash
python scripts/build_author_work_mapping.py
python scripts/lookup_viaf.py                 # optional
python manage.py import_csv_data
python manage.py seed_mlaib_elo
python scripts/lookup_wikipedia.py            # optional
python manage.py run_llm_elo --mode both --count 200
python manage.py build_static
```

## Repository Layout

```
config/            Django project settings and URLs (local SQLite only)
core/              the single Django app: models, views, ELO logic, management commands, migrations
templates/         Django templates (top-level, not inside core/)
static/            CSS/JS source; build_static copies it into _site/static/
prompts/           LLM system prompt used by submit_llm_batch / run_llm_elo / test_self_consistency
writing/           drafts of the About page text (about.md, pandoc-style about.html); the live copy is templates/about.html
fixtures/          small sample data for loaddata
scripts/           offline MLAIB data-pipeline scripts
tests/             all tests (pytest + pytest-django)
_site/             gitignored build output; published to the gh-pages branch with ghp-import
data/              gitignored raw/intermediate data (MLAIB XML, CSVs, lookup caches)
backups/           gitignored manual SQLite backups (db.<date>.sqlite3)
db.sqlite3         gitignored dev database
```

### Reorganization (November 2026)

What moved, so old paths and habits still make sense:

| Before | After | Notes |
|--------|-------|-------|
| `core/tests.py` | `tests/test_core.py` | Was never collected by `pytest`; now is. |
| `tests/conftest.py` | removed | Replaced by `pytest-django` config in `pyproject.toml` (`DJANGO_SETTINGS_MODULE`, `pythonpath`), so plain `pytest` works and Django tests use a throwaway test DB. |
| `deploy_mysql.sh`, `setup_mysql_db.sql`, `requirements-prod.txt` | `deploy/mysql/` | `deploy_mysql.sh` now `cd`s to the repo root itself, and runs `manage.py check` instead of a test module that no longer existed. |
| `db.<date>*.sqlite3` (repo root) | `backups/` | Local only; gitignored. |
| `_site/`, root `__pycache__/` | deleted | Stale, gitignored build output and bytecode. Regenerate with `build_static -o _site` if wanted. |
| `docs/` on `main` | `_site/` → `gh-pages` branch | `build_static` now defaults to `_site/` and the site is deployed with `ghp-import -n -p _site`, keeping generated HTML out of `main`. |
| `config/settings_production.py`, `deploy/mysql/` | removed | There is no hosted server or database; the site is static. `.env.example` now lists only the LLM API keys. |
| `core/managers.py`, `search_static.html` | removed | Search is client-side everywhere: `search.html` filters `search-data.json` (from `core/search_index.py`). |

Test fixes made alongside: the `import_csv_data` tests still passed the removed `--min-count` option; they now use `top_pct=100` and supply a placeholder work (the command requires one). One assertion changed to match current behavior: under `--top-pct`, an author with no `mlaib_record_count` is not imported at all.
