# Canon Wars: Literary Canonicity Rankings

A literary canonicity ranking system that uses ELO ratings to rank authors and works through LLM-judged head-to-head comparisons. Rankings are generated locally using an offline data pipeline and published as a static site on GitHub Pages.

## How It Works

### ELO Algorithm

- Standard ELO with K-factor of 32 and default starting rating of 1200
- New items are seeded with an informed prior (`mlaib_elo`) derived from MLAIB citation-count z-scores (range 800–1600) before matchups run

### LLM Matchups

The `run_llm_elo` management command pairs authors (or works), asks an LLM to judge which is more canonical, and writes results directly to the database. Each judgment is persisted as an `LLMMatchup` record with before/after ELO values and the model used.

**Pairing algorithm** — each batch run maximizes information gain per comparison using three factors:

1. *ELO proximity* — pairs with similar ratings are preferred, since near-equal matchups keep the win probability close to 50% and thus maximize binary entropy per comparison: `exp(-elo_diff / 200)`.
2. *Item novelty* — under-compared items are preferred via `1 / sqrt(games_played + 1)`, directing the budget toward unexplored items before revisiting settled ones.
3. *Pair novelty* — previously judged pairs are downweighted via `1 / sqrt(pair_count + 1)`, so historical matchups are soft-penalized rather than hard-blocked.

The combined weight for each candidate pair is `elo_proximity × novelty_b × pair_novelty`. Item A is also sampled proportionally to its own novelty weight. Within a single batch run, each pair appears at most once.

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

`python manage.py build_static` renders the entire site to `docs/` as flat HTML files:

- Leaderboards (paginated, one HTML file per page)
- Author and work detail pages
- Comparison history pages
- Recent results
- Client-side search (accent-insensitive, with rank context display)

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

# Build the static site
python manage.py build_static

# Preview locally
python -m http.server -d docs

# Deploy to GitHub Pages (requires ghp-import)
ghp-import -n -p docs
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

## Production Deployment (MySQL)

The live site is static (GitHub Pages), so this is only needed for a server-hosted deployment. The files live in `deploy/mysql/`.

```bash
cp .env.example .env
# fill in DB_NAME, DB_USER, DB_PASSWORD, SECRET_KEY, ALLOWED_HOSTS
mysql -u root -p < deploy/mysql/setup_mysql_db.sql
pip install -r deploy/mysql/requirements-prod.txt
./deploy/mysql/deploy_mysql.sh
```

Set `DJANGO_SETTINGS_MODULE=config.settings_production` for the production settings.

## Repository Layout

```
config/            Django project settings and URLs (settings.py dev/SQLite, settings_production.py MySQL)
core/              the single Django app: models, views, ELO logic, management commands, migrations
templates/         Django templates (top-level, not inside core/)
static/            CSS/JS source; build_static copies it into docs/static/
prompts/           LLM system prompt used by run_llm_elo / test_self_consistency
writing/           drafts of the About page text (about.md, pandoc-style about.html); the live copy is templates/about.html
fixtures/          small sample data for loaddata
scripts/           offline MLAIB data-pipeline scripts
tests/             all tests (pytest + pytest-django)
deploy/mysql/      optional MySQL server deployment: setup SQL, prod requirements, setup script
docs/              GENERATED static site for GitHub Pages — rebuild with build_static, don't hand-edit
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

Test fixes made alongside: the `import_csv_data` tests still passed the removed `--min-count` option; they now use `top_pct=100` and supply a placeholder work (the command requires one). One assertion changed to match current behavior: under `--top-pct`, an author with no `mlaib_record_count` is not imported at all.
