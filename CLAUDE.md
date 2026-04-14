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

# Cleanup old comparison records
python manage.py cleanup_comparisons
```

The project uses `uv` for dependency management (`pyproject.toml`). Dev uses SQLite; production uses MySQL via `config/settings_production.py` and requires env vars (`DB_NAME`, `DB_USER`, `DB_PASSWORD`, etc.) as shown in `.env.example`.

## Architecture

Django app with a single `core` app. `config/urls.py` delegates everything to `core.urls` (function-based views) and `core.urls_cbv` (class-based views). The active URL file is `core/urls.py` — if you add views, prefer the CBV pattern in `views_cbv.py` and register them in `urls_cbv.py`.

**Key files:**
- `core/models.py` — `Author`, `Work`, `Comparison` models. All ELO ratings stored as `FloatField` defaulting to 1200.
- `core/elo.py` — Pure functions `expected()` and `update()`. K-factor and defaults live in `core/constants.py`.
- `core/business.py` — Three services: `PairingService` (weighted random selection by ELO proximity), `ComparisonService` (atomic ELO update), `SearchService` (accent-insensitive search with rank context).
- `core/managers.py` — Custom querysets with `by_elo_rating()` and `search()`. Search falls back to Python-side unicode normalization for SQLite; uses MySQL collation in production.
- `core/views_cbv.py` — CBV wrappers that delegate entirely to the service layer.

**Voting flow:** `GET /compare/<mode>/` with `?winner=A|B&item_a_id=N&item_b_id=N` → `ComparisonService.record_comparison()` updates both ELO ratings atomically → redirects to a fresh comparison (PRG pattern to prevent duplicate votes).

**Pairing algorithm:** `PairingService.get_two_by_elo()` picks item A randomly, then weights item B candidates using `exp(-elo_diff / 100)` and applies a `0.1` multiplier to pairs compared within the last 6 hours (`COMPARISON_PENALTY_HOURS`).

## Data pipeline scripts

Scripts in `scripts/` operate on CSV files in `data/` (gitignored) and are independent of Django:

- `generate_author_elo.py` — Reads `data/mlaib_authors.csv`, z-scores work counts, scales to ELO range 1000–3000, writes `data/elo/author_elo_0.csv`
- `generate_pairings.py` — Generates author pairings CSV
- `evaluate_pairings.py` — Calls OpenAI (structured output via Pydantic `Verdict` model) to judge pairings; uses `prompts/authors-system-v4.md` by default
- `generate_and_evaluate_pairings.py` — Combines the two steps above
- `update_author_elo.py` — Applies verdicts back to ELO ratings

The evaluation scripts require `OPENAI_API_KEY` in the environment (loaded via `python-dotenv`).
