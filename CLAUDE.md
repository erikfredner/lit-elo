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

# Seed Elo priors from log-scaled MLAIB citation counts (run after import_csv_data; then rebuild_ratings if items have played)
python manage.py seed_mlaib_elo [--calibrate] [--dry-run]

# Back-fill VIAF IDs from data/authors.csv into Author records
python manage.py update_viaf_ids [--dry-run]

# Batch LLM matchups via OpenRouter (requires OPENROUTER_API_KEY in .env) — the main pipeline
python manage.py submit_llm_batch --mode both --count 50 [--models a/b,c/d] [--votes-per-pairing 2] [--dry-run] [--max-cost 2]
python manage.py collect_llm_batches [--wait]   # poll OpenRouter; apply finished batches to all Elo ladders
python manage.py rebuild_ratings                # replay all LLMMatchups from priors (aggregate + per-model + per-maker)
python manage.py submit_llm_batch --mode both --games-per-item 5 --sync         # same requests as ordinary calls (~2x price), applied at once
python manage.py submit_llm_batch --mode authors --top 150 --games-per-item 10 --sync   # top-tier round: pair only the top N by the fit
python manage.py submit_llm_batch --mode both --models typesafe/jev-1.14 --prompt plain --replay-model anthropic/claude-haiku-5.5 --sync   # prompt experiment (prompts/plain.md)
python manage.py fit_ratings                    # refit Bradley–Terry ratings + rank intervals (also run by collect, --sync, rebuild, build_static)

# Legacy sync LLM ELO matchups (requires OPENAI_API_KEY, loaded from .env)
python manage.py run_llm_elo --mode authors --count 50     # --mode authors|works|both
python manage.py run_llm_elo --mode works --count 50 --dry-run   # print prompts + token estimates, no API calls
# Options: --model (default gpt-5.4-nano), --concurrency, --seed, --reps,
#          --votes-per-pairing (default 3, majority wins), --system-prompt, --exclude-overrepresented

# Diagnostics (read-only; do not modify the DB)
python manage.py test_self_consistency --mode authors --count 10 --dry-run   # model agreement with itself across reps
python manage.py analyze_convergence --mode authors --output-csv /tmp/conv.csv  # upset rate / mean |ΔELO| per batch

# Static site (output dir _site/ is gitignored; deploy pushes it to the gh-pages branch)
python manage.py build_static           # outputs to _site/  (-o DIR for another dir)
python -m http.server -d _site          # preview the built files
ghp-import -n -p _site                  # deploy to GitHub Pages (ghp-import is a dev dependency)
```

The only database is local SQLite (`db.sqlite3`; dated manual backups go in gitignored `backups/`). There is no production server or database: the published site is the static build. API keys go in `.env` (see `.env.example`).

Django tests (`tests/test_core.py`) use `django.test.TestCase`; pytest-django gives them a temporary test DB. Don't put tests back in `core/tests.py` — pytest won't collect them there.

See "Repository Layout" in README.md for the directory map and the November 2026 reorganization.

## Architecture

Django app with a single `core` app. `config/urls.py` delegates to `core.urls`, which registers all function-based views in `core/views.py`.

- `core/models.py` — `Author`, `Work` (FK to Author), `LLMMatchup`. Author/Work carry two ELO fields: `elo_rating` (live matchup rating, default 1200) and `mlaib_elo` (prior: `1200 + slope·(ln count − mean ln count)` over the DB items (max count over a name's CSV rows: the CSV has homonyms), slope 350 Elo per e-fold or `--calibrate`d from the fit; counts follow a power law, so a linear scale left the prior flat). `seed_mlaib_elo` sets `elo_rating = mlaib_elo` for items with no published matchups; played items keep their Elo until `rebuild_ratings`. `LLMMatchup` stores one row per judgment (content type, item A/B PKs, winner, ELO before/after, model) and is the source of truth for history, comparison pages, and the diagnostics commands.
- `core/models.py` (cont.) — `LLMMatchup.score_a` is the fractional result for A (mean P(A) over order-swapped votes; null for legacy binary rows, see `result_a`) and `votes` holds the raw per-vote records. `LLMBatch` tracks one OpenRouter batch (model × content type), including the pairing `manifest` needed to apply results later. `ItemRating` holds per-scope Elo, where a scope is `model:<openrouter slug>` or `maker:<maker>`.
- `SystemPrompt` — named prompt text (`prompts/<name>.md`, registered on first use by `judging.resolve_system_prompt`; a registered name's text can't change). `LLMBatch` and `LLMMatchup` both FK to it. Exactly one is `is_default` (`professor`); only its matchups are published. `LLMMatchup.objects` is filtered to the default prompt (or null, from legacy `run_llm_elo`), so views, ladders, fits and pairing ignore experiments; use `LLMMatchup.all_objects` (also the default/related manager) for every prompt. Matchups under another prompt update no aggregate/model/maker ladder, only `model:<slug>@<prompt>` (Elo and fit), which `available_scopes` and item pages skip. `--replay-model` replays only default-prompt source batches and resumes per prompt. `--replicate` registers a prompt whose text duplicates another's (e.g. `professor-retest`, a test–retest noise baseline).
- `core/judging.py` — prompt/schema, vote parsing, logprob→probability, order alternation, scoring. `core/openrouter.py` — stdlib Batch API client. `core/ratings.py` — scopes, `RatingBook` (applies one result to the aggregate and scoped ladders, starting from the `mlaib_elo` prior), `rebuild_ratings`. `core/rankings.py` — per-scope leaderboard/judge-stat queries shared by views and `build_static`.
- `core/bt.py` — Bradley–Terry fit over all matchups (order-independent), with standard errors and posterior rank intervals; `refit()` writes `FittedRating` rows per scope (`all`, `model:…`, `maker:…`). Leaderboards, detail pages and the search index are ordered by the fit and show rating ± 95% and "likely rank"; they fall back to Elo order when no fit exists. Elo ladders remain for the history charts and for pairing in `run_llm_elo` (and `submit_llm_batch --pair-by elo`).
- `core/elo.py` — pure `expected()` / `update()`; K-factor and defaults in `core/constants.py`.
- `core/views.py` — FBVs, and the only renderer for every page (`build_static` just requests them). Pagination is path-based (`…/page/N/`), never `?page=`, so pages can be frozen to flat files. Ranks are leaderboard positions (Elo ties broken by name/title, or by `item_id` within a scope).
- `core/search_index.py` — `build_search_index()`, served as `/search-data.json` and written to the static build.
- `templates/` — top-level (not inside `core/`).

### LLM matchups (`run_llm_elo`)

Uses the OpenAI **Responses API** (`client.responses.parse` with a Pydantic `_Verdict` schema), not Chat Completions. System prompt: `prompts/professor.md`. Each pairing gets `--votes-per-pairing` calls and the majority verdict is applied. The first item of each pair is always a least-played item (history plus the batch so far), which keeps games even across the ladder; its opponent is weighted by ELO proximity (`exp(-diff/200)`) × per-item novelty (`1/sqrt(games+1)`) × per-pair novelty (`1/sqrt(pair_count+1)`). A pair appears at most once per batch, and `_load_matchup_index` counts games per pairing (max over judge models). `test_self_consistency` imports its pairing/prompt helpers from `run_llm_elo`, so changes to those private functions affect both. `analyze_convergence` compares batch upset rates against the model's self-inconsistency noise floor (~13%) to judge whether more runs are still informative.

### Batch matchups (`submit_llm_batch` / `collect_llm_batches`)

OpenRouter batch requests are inline JSON (no JSONL file), and `requests` must be the last key. Results come back unordered (match on `custom_id` `p{pair}-v{vote}`) and can take up to 24h (Google up to 96h). Pairing reuses `_generate_pairings` from `run_llm_elo`, drawn once and shared across models, but opponent proximity uses the pooled Bradley–Terry fit (`pairing_ratings`: items with no games keep their Elo, i.e. the MLAIB prior) rather than the Elo ladder, which lags the fit; `--pair-by elo` restores the ladder. Ratings are applied when a batch is collected, not when it is submitted, so Elo "before" values reflect the state at apply time.

Decision models (OpenRouter output modality `decisions`, e.g. `typesafe/jev-1.14`) reject chat completions and are missing from the default `/models` listing. Each vote is one `choice` question (options `"1"`/`"2"`) on `/api/alpha/decisions` (`openrouter.decide`, `judging.build_decision_body`); `apply_results` reads `answers` instead of `choices`. They have no Batch API, so they need `--sync`. Votes are binary like every other judge; the model's probability is kept as `p_decision` and is used for scoring only with `--logprobs`.

### Static site

The dynamic site is the preview of the static one. `build_static` lists every URL (`site_urls()`), fetches each through Django's test client, and writes the response to `<out>/<path>/index.html`, rewriting root-relative `href`/`src` to relative paths so the site works under a GitHub Pages subpath. Adding a page means adding a view plus its URL(s) to `site_urls()`; don't render templates in `build_static`.

Search is client-side everywhere: `templates/search.html` fetches `../search-data.json` (relative, so it works in dev and under any base path) and filters in JavaScript, including accent-insensitive matching. There is no server-side search.

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
