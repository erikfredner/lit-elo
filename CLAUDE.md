# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

"Canon Wars": ranks American literary authors and works by canonicity using ELO ratings driven by LLM-judged head-to-head comparisons. Rankings are computed locally against a SQLite DB and published as a pre-rendered static site on GitHub Pages; the live site has no server.

## Commands

```bash
uv sync                      # install deps (Python 3.13+, uv-managed via pyproject.toml)
python manage.py runserver   # dev server

# Tests (pytest + pytest-django, configured in pyproject.toml; all tests live in tests/)
pytest
pytest tests/test_elo.py
pytest tests/test_elo.py::test_name

# Database
python manage.py migrate
python manage.py loaddata fixtures/authors.json fixtures/works.json

# Import authors/works from data/authors.csv and data/works.csv (skips existing records)
python manage.py import_csv_data                 # top 5% by mlaib_record_count (--top-pct N)
python manage.py import_csv_data --works FILE    # custom works CSV; imports only its referenced authors
python manage.py import_csv_data --dry-run

# Seed ELO ratings from MLAIB citation counts (run after import_csv_data)
python manage.py seed_mlaib_elo [--dry-run]

# Back-fill VIAF IDs from data/authors.csv into Author records
python manage.py update_viaf_ids [--dry-run]

# LLM ELO matchups (requires OPENAI_API_KEY, loaded from .env)
python manage.py run_llm_elo --mode authors --count 50     # --mode authors|works|both
python manage.py run_llm_elo --mode works --count 50 --dry-run   # print prompts + token estimates, no API calls
# Options: --model (default gpt-5.4-nano), --concurrency, --seed, --reps,
#          --votes-per-pairing (default 3, majority wins), --system-prompt, --exclude-overrepresented

# Diagnostics (read-only; do not modify the DB)
python manage.py test_self_consistency --mode authors --count 10 --dry-run   # model agreement with itself across reps
python manage.py analyze_convergence --mode authors --output-csv /tmp/conv.csv  # upset rate / mean |ΔELO| per batch

# Static site
python manage.py build_static           # outputs to docs/
python manage.py build_static -o _site  # custom output dir
python -m http.server -d docs           # preview
ghp-import -n -p docs                   # deploy to GitHub Pages
```

Dev uses SQLite (`db.sqlite3`; dated manual backups go in gitignored `backups/`). Production uses MySQL via `config/settings_production.py` (`DJANGO_SETTINGS_MODULE=config.settings_production`) and requires env vars from `.env.example`; setup SQL, prod requirements and `deploy_mysql.sh` are in `deploy/mysql/`.

Django tests (`tests/test_core.py`) use `django.test.TestCase`; pytest-django gives them a temporary test DB. Don't put tests back in `core/tests.py` — pytest won't collect them there.

See "Repository Layout" in README.md for the directory map and the November 2026 reorganization.

## Architecture

Django app with a single `core` app. `config/urls.py` delegates to `core.urls`, which registers all function-based views in `core/views.py`.

- `core/models.py` — `Author`, `Work` (FK to Author), `LLMMatchup`. Author/Work carry two ELO fields: `elo_rating` (live matchup rating, default 1200) and `mlaib_elo` (prior from MLAIB citation-count z-scores, range 800–1600). `seed_mlaib_elo` sets `elo_rating = mlaib_elo` for items still at the default. `LLMMatchup` stores one row per judgment (content type, item A/B PKs, winner, ELO before/after, model) and is the source of truth for history, comparison pages, and the diagnostics commands.
- `core/elo.py` — pure `expected()` / `update()`; K-factor and defaults in `core/constants.py`.
- `core/managers.py` — custom querysets with `search()`. Falls back to Python-side unicode normalization on SQLite; uses MySQL collation in production.
- `core/views.py` — FBVs. The search view loads all items into memory and filters in Python (search-with-rank-context pattern).
- `templates/` — top-level (not inside `core/`).

### LLM matchups (`run_llm_elo`)

Uses the OpenAI **Responses API** (`client.responses.parse` with a Pydantic `_Verdict` schema), not Chat Completions. System prompt: `prompts/system-prompt.md`. Each pairing gets `--votes-per-pairing` calls and the majority verdict is applied. Pairings are weighted by ELO proximity (`exp(-diff/200)`) × per-item novelty (`1/sqrt(games+1)`) × per-pair novelty (`1/sqrt(pair_count+1)`), so close matches between under-played items are preferred; a pair appears at most once per batch. `test_self_consistency` imports its pairing/prompt helpers from `run_llm_elo`, so changes to those private functions affect both. `analyze_convergence` compares batch upset rates against the model's self-inconsistency noise floor (~13%) to judge whether more runs are still informative.

### Static vs. dynamic site

`build_static` renders everything (paginated leaderboards, detail pages, comparison history, recent results) to flat HTML. The one behavioral difference is **search**: the dynamic site searches server-side via `core/managers.py` using `search.html`; the static build emits `search-data.json` and renders `search_static.html`, which filters client-side in JavaScript. Changes to search behavior usually need to be made in both places.

## Data pipeline scripts

Scripts in `scripts/` operate on files in `data/` (gitignored):

- `build_author_work_mapping.py` — processes XML records in `data/mlaib_data/` with multiprocessing; outputs `author_work_mapping.csv`, `author_presence.csv`, and the import-ready `authors.csv` and `works.csv`. Uses parsing helpers from `normalize_mlaib.py`. Independent of Django.
- `lookup_viaf.py` — enriches `data/authors.csv` with VIAF IDs via the VIAF AutoSuggest API; resumable via `data/.viaf_cache.json`. Independent of Django.
- `lookup_wikipedia.py` — **uses Django**: reads Authors with a `viaf_id` from the DB, fetches English Wikipedia URLs from VIAF justlinks, writes `Author.wikipedia_url`; resumable via `data/.wikipedia_cache.json` (`--limit`, `--dry-run`, `--force`).

Typical pipeline:

```bash
python scripts/build_author_work_mapping.py   # → authors.csv, works.csv
python scripts/lookup_viaf.py                 # optional: VIAF IDs in authors.csv
python manage.py import_csv_data
python manage.py seed_mlaib_elo
python scripts/lookup_wikipedia.py            # optional: needs viaf_id in DB
python manage.py run_llm_elo --mode both --count 200
python manage.py build_static
```
