# Lit-ELO: Literary Canonicity Ranking

A Django application that ranks literary authors and works by canonicity using pairwise ELO comparisons. Two sources drive the rankings: user votes through the web interface and LLM-judged matchups run via a management command.

## How It Works

### ELO Algorithm

- Standard ELO with K-factor of 32 and default starting rating of 1200

### User Voting

1. Two authors or works are displayed side-by-side
2. User clicks their preferred choice
3. ELO ratings update atomically; page redirects to prevent duplicate votes on refresh

### LLM Matchups

The `run_llm_elo` management command pairs authors (or works) by ELO proximity, asks an LLM to judge which is more canonical, and writes results directly to the database. Each judgment is persisted as an `LLMMatchup` record with before/after ELO values and the model used.

```bash
python manage.py run_llm_elo --mode authors --count 50
python manage.py run_llm_elo --mode works   --count 50
python manage.py run_llm_elo --mode authors --count 10 --dry-run  # preview prompts, no API calls
```

Default model: `gpt-5.4-nano`. Override with `--model <id>`. Requires `OPENAI_API_KEY` in the environment (or `.env` file).

### URL Structure

- `/` — redirects to a random author comparison
- `/compare/authors/` and `/compare/works/` — pairwise voting
- `/leaderboard/authors/` and `/leaderboard/works/` — ELO rankings
- `/search/` — accent-insensitive search with ranking context

## Quick Start

Requires Python 3.13+ and Django 5.2+.

```bash
git clone <repository-url>
cd lit-elo
uv sync                          # or: pip install -r requirements-prod.txt
python manage.py migrate
python manage.py loaddata fixtures/authors.json fixtures/works.json
python manage.py runserver
```

## Database Models

- **Author** — name, birth/death years, ELO rating
- **Work** — title, author (FK), publication year, form, ELO rating
- **LLMMatchup** — one record per LLM judgment: content type, item A/B PKs, winner, ELO before/after, model used, timestamp
- **Comparison** — tracks recent user-vote pairs to limit repetition in the pairing algorithm

## Running Tests

```bash
pytest
# or
python manage.py test
```

## Production Deployment (MySQL)

```bash
cp .env.example .env
# fill in DB_NAME, DB_USER, DB_PASSWORD, SECRET_KEY, ALLOWED_HOSTS
mysql -u root -p < setup_mysql_db.sql
./deploy_mysql.sh
```

Set `DJANGO_SETTINGS_MODULE=config.settings_production` for the production settings.

## Data Pipeline Scripts

Standalone scripts in `scripts/` process the raw MLAIB bibliography data. They are independent of Django and operate on CSV files in `data/`.

| Script | Purpose |
|--------|---------|
| `normalize_mlaib.py` | Parse `mlaib_authors.csv` and `mlaib_works.csv` into clean relational tables (`data/authors.csv`, `data/works.csv`) |
| `generate_author_elo.py` | Convert MLAIB work counts to initial ELO scores via z-score scaling |
| `generate_pairings.py` | Generate ELO-proximity-weighted author pairings as CSV |
| `evaluate_pairings.py` | Call OpenAI to judge pairings; writes verdicts back to the CSV |
| `update_author_elo.py` | Apply CSV verdicts to produce an updated ELO CSV |
| `generate_and_evaluate_pairings.py` | Orchestrate the three steps above |

These scripts produce intermediate CSV files used for bootstrapping or offline analysis. For ongoing ELO updates, prefer the `run_llm_elo` management command, which writes directly to the database.

## License

MIT — see LICENSE.
