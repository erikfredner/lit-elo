# Canon Wars: Literary Canonicity Rankings

A literary canonicity ranking system that uses ELO ratings to rank authors and works through LLM-judged head-to-head comparisons. Rankings are generated locally using an offline data pipeline and published as a static site on GitHub Pages.

## How It Works

### ELO Algorithm

- Standard ELO with K-factor of 32 and default starting rating of 1200

### LLM Matchups

The `run_llm_elo` management command pairs authors (or works), asks an LLM to judge which is more canonical, and writes results directly to the database. Each judgment is persisted as an `LLMMatchup` record with before/after ELO values and the model used.

**Pairing algorithm** — each batch run maximizes information gain per comparison using two factors:

1. *ELO proximity* — pairs with similar ratings are preferred, since near-equal matchups keep the win probability close to 50% and thus maximize binary entropy per comparison: `exp(-elo_diff / 200)`.
2. *Novelty* — under-compared items are preferred via `1 / sqrt(games_played + 1)`, directing the budget toward unexplored items before revisiting settled ones.

The combined weight for each candidate pair is `elo_proximity × novelty_a × novelty_b`. Historical matchups are loaded from the `LLMMatchup` table at startup so pairs already judged in previous runs are skipped entirely.

```bash
python manage.py run_llm_elo --mode authors --count 50
python manage.py run_llm_elo --mode works   --count 50
python manage.py run_llm_elo --mode authors --count 10 --dry-run  # preview prompts, no API calls
```

Default model: `gpt-5.4-nano`. Override with `--model <id>`. Requires `OPENAI_API_KEY` in the environment (or `.env` file).

### Static Site

`python manage.py build_static` renders the entire site to `_site/` as flat HTML files:

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
python -m http.server -d _site

# Deploy to GitHub Pages (requires ghp-import)
ghp-import -n -p _site
```

## Database Models

- **Author** — name, birth/death years, ELO rating
- **Work** — title, author (FK), publication year, form, ELO rating
- **LLMMatchup** — one record per LLM judgment: content type, item A/B PKs, winner, ELO before/after, model used, timestamp

## Running Tests

```bash
python manage.py test
```

## Data Pipeline Scripts

Standalone scripts in `scripts/` process the raw MLAIB bibliography data. They are independent of Django and operate on CSV files in `data/` (gitignored).

| Script | Purpose |
|--------|---------|
| `generate_pairings.py` | Generate ELO-proximity-weighted author pairings as CSV |
| `evaluate_pairings.py` | Call OpenAI to judge pairings; writes verdicts back to the CSV |
| `update_author_elo.py` | Apply CSV verdicts to produce an updated ELO CSV |
| `generate_and_evaluate_pairings.py` | Orchestrate the three steps above |

For ongoing ELO updates, prefer the `run_llm_elo` management command, which writes directly to the database.

## Production Deployment (MySQL)

```bash
cp .env.example .env
# fill in DB_NAME, DB_USER, DB_PASSWORD, SECRET_KEY, ALLOWED_HOSTS
mysql -u root -p < setup_mysql_db.sql
./deploy_mysql.sh
```

Set `DJANGO_SETTINGS_MODULE=config.settings_production` for the production settings.

## License

MIT — see LICENSE.
