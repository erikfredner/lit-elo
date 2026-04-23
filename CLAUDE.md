# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Development server
python manage.py runserver

# Run all tests
pytest
# Run a single test file
pytest tests/test_elo.py
# Run Django tests
python manage.py test

# Database
python manage.py migrate
python manage.py loaddata fixtures/authors.json fixtures/works.json

# Import authors/works from data/authors.csv and data/works.csv (skips existing records)
python manage.py import_csv_data
python manage.py import_csv_data --dry-run

# Run LLM ELO matchups (requires OPENAI_API_KEY)
python manage.py run_llm_elo --mode authors --count 50
python manage.py run_llm_elo --mode works --count 50 --dry-run
# Options: --model, --concurrency, --seed, --reps, --system-prompt, --exclude-overrepresented

# Build static site for GitHub Pages
python manage.py build_static           # outputs to docs/
python manage.py build_static -o _site  # custom output dir

# Deploy static site (requires ghp-import)
ghp-import -n -p docs

# Preview static site locally
python -m http.server -d docs
```

The project uses `uv` for dependency management (`pyproject.toml`). Dev uses SQLite; production uses MySQL via `config/settings_production.py` and requires env vars (`DB_NAME`, `DB_USER`, `DB_PASSWORD`, etc.) as shown in `.env.example`.

## Architecture

Django app with a single `core` app. `config/urls.py` delegates to `core.urls`, which registers all function-based views in `core/views.py`.

**Key files:**
- `core/models.py` — `Author`, `Work`, `LLMMatchup` models. All ELO ratings stored as `FloatField` defaulting to 1200.
- `core/elo.py` — Pure functions `expected()` and `update()`. K-factor and defaults live in `core/constants.py`.
- `core/managers.py` — Custom querysets with `by_elo_rating()` and `search()`. Search falls back to Python-side unicode normalization for SQLite; uses MySQL collation in production.
- `core/views.py` — All views are FBVs. Search view loads all items into memory and filters in Python (search-with-rank-context pattern).

## Data pipeline scripts

Scripts in `scripts/` operate on CSV files in `data/` (gitignored) and are independent of Django.

**Data preparation (run once, or when mlaib_data changes):**

- `build_author_work_mapping.py` — Processes individual XML records in `data/mlaib_data/` using multiprocessing. Outputs four files: `author_work_mapping.csv`, `author_presence.csv`, `authors.csv`, and `works.csv`. The last two are the import-ready CSVs consumed by `import_csv_data`.
- `normalize_mlaib.py` — Parsing helpers (`parse_author_field`, `parse_work_field`) imported by `build_author_work_mapping.py`.
- `lookup_viaf.py` — Enriches `data/authors.csv` with VIAF authority IDs (queries VIAF AutoSuggest API; uses `data/.viaf_cache.json` for resumability). Run after `build_author_work_mapping.py`.

**ELO pairing scripts (legacy; prefer `run_llm_elo` management command):**

- `generate_pairings.py` — Generates author pairings CSV
- `evaluate_pairings.py` — Calls OpenAI (structured output via Pydantic `Verdict` model) to judge pairings; uses `prompts/authors-system-v4.md` by default
- `generate_and_evaluate_pairings.py` — Combines the two steps above
- `update_author_elo.py` — Applies verdicts back to ELO ratings

The evaluation scripts require `OPENAI_API_KEY` in the environment (loaded via `python-dotenv`). These standalone scripts predate the `run_llm_elo` management command, which is the preferred way to run matchups against the Django database.

**Typical data pipeline:**

```bash
python scripts/build_author_work_mapping.py   # → authors.csv, works.csv (+ mapping files)
python scripts/lookup_viaf.py                 # enriches authors.csv with VIAF IDs (optional)
python manage.py import_csv_data              # → Django DB
```

## Static site vs. dynamic site differences

The static build (`build_static`) differs from the live Django app in one key area: **search**. The dynamic site does server-side search via `core/managers.py`. The static build emits `search-data.json` (all authors and works serialized) and renders `search_static.html`, which uses client-side JavaScript to filter results. `search.html` (server-side) is only used in the dynamic app.

The system prompt for `run_llm_elo` lives at `prompts/system-prompt.md`.
