# Lit-ELO: Literary Canonicity Ranking

Lit-EL## How it Works

### Voting System

1. Two items (authors or works) are displayed with basic info
2. User can choose:
   - First item (left choice)
   - Second item (right choice)
   - Tie (equal canonicity)
3. ELO ratings update using the algorithm in `core/elo.py`
4. Page redirects to prevent duplicate votes on refresh

### ELO Algorithm

- Uses standard ELO rating calculation
- K-factor of 32 for meaningful rating changes
- Default starting rating of 1200
- Supports draws/ties (result = 0.5)
- Equal-rated items that tie maintain their ratings
- Differently-rated items adjust toward each other on tiesapplication that ranks literary authors and works by canonicity using pairwise ELO comparisons. Users compare two authors or works, and their choices update the ELO ratings.

## Features

- **Pairwise Comparisons**: Simple click-based voting on author or work pairs
- **Tie Voting**: Option to vote that two items have equal canonicity
- **ELO Rating System**: Dynamic rankings based on comparison outcomes (including ties)
- **Leaderboards**: Separate rankings for authors and works with pagination
- **Search Functionality**: Find and view specific authors/works with context (accent-insensitive)
- **Wikipedia Integration**: Custom Wikipedia URLs with automatic fallbacks
- **Responsive Design**: Works on desktop and mobile devices
- **MySQL Support**: Production-ready with MySQL database optimization

## Quick Start

This project requires Python 3.11+ and Django 5.0+.

1. **Clone and setup:**

   ```bash
   git clone <repository-url>
   cd lit-elo
   pip install -r requirements-prod.txt  # or use uv
   ```

2. **Database setup:**

   ```bash
   python manage.py migrate
   python manage.py loaddata fixtures/authors.json fixtures/works.json
   ```

3. **Run development server:**

   ```bash
   python manage.py runserver
   ```

4. **Visit the application:**
   - Main app: `http://127.0.0.1:8000/`
   - Author leaderboard: `http://127.0.0.1:8000/leaderboard/authors/`
   - Work leaderboard: `http://127.0.0.1:8000/leaderboard/works/`
   - Search: `http://127.0.0.1:8000/search/`

## How It Works

### Voting System

1. Two items (authors or works) are displayed with basic info
2. User clicks their preferred choice (simple hyperlink)
3. ELO ratings update using the algorithm in `core/elo.py`
4. Page redirects to prevent duplicate votes on refresh

### ELO Algorithm

- Uses standard ELO rating calculation
- K-factor of 32 for meaningful rating changes
- Default starting rating of 1200

### URL Structure

- `/` - Home page (redirects to random comparison)
- `/compare/authors/` - Compare two random authors
- `/compare/works/` - Compare two random works
- `/leaderboard/authors/` - Author rankings
- `/leaderboard/works/` - Work rankings
- `/search/` - Search authors and works (accent-insensitive, e.g., "marquez" finds "Márquez")

## Production Deployment with MySQL

### Environment Configuration

Create a `.env` file based on `.env.example`:

```bash
cp .env.example .env
# Edit .env with your database credentials
```

### MySQL Setup

1. **Install MySQL 8.0+**
2. **Create database and user:**

   ```bash
   mysql -u root -p < setup_mysql_db.sql
   ```

3. **Deploy with script:**

   ```bash
   chmod +x deploy_mysql.sh
   ./deploy_mysql.sh
   ```

### Manual MySQL Deployment

1. **Install dependencies:**

   ```bash
   pip install -r requirements-prod.txt
   ```

2. **Set environment variables:**

   ```bash
   export DJANGO_SETTINGS_MODULE=config.settings_production
   export DB_NAME=canonwars_db
   export DB_USER=canonwars_user
   export DB_PASSWORD=your_secure_password
   export DB_HOST=localhost
   export DB_PORT=3306
   export SECRET_KEY=your_secret_key
   export ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com
   ```

3. **Run migrations and collect static files:**

   ```bash
   python manage.py migrate
   python manage.py collectstatic --noinput
   python manage.py loaddata fixtures/authors.json fixtures/works.json
   ```

### MySQL Optimizations

The application includes several MySQL-specific optimizations:

- Strategic database indexes on frequently queried fields
- Optimized queries for ELO ranking operations
- utf8mb4 charset with proper collation
- Production-ready security configurations

## Development

### Running Tests

```bash
python manage.py test
# or
pytest
```

### Project Structure

```
lit-elo/
├── config/              # Django settings
├── core/                # Main application
│   ├── models.py        # Author, Work, Comparison models
│   ├── views.py         # Core views
│   ├── elo.py          # ELO calculation logic
│   ├── services.py     # Business logic
│   └── managers.py     # Custom model managers
├── templates/           # HTML templates
├── static/             # CSS and static files
├── fixtures/           # Initial data
└── scripts/            # Utility scripts
```

### Key Components

- **ELO System**: Implemented in `core/elo.py` with customizable K-factor
- **Pairing Algorithm**: Intelligent pairing based on ELO proximity for competitive matches
- **Custom Managers**: Optimized database queries in `core/managers.py`
- **Wikipedia Integration**: Conditional display of Wikipedia links when custom URLs are provided

### Utility Scripts

The repository includes several utility scripts:

- `populate_wikipedia_urls.py` - Add Wikipedia URLs to existing data
- `generate_test_data.py` - Create additional test data for pagination
- `scripts/bootstrap_real_db.py` - Bootstrap production database

## Architecture Notes

### Database Models

- **Author**: Name, birth/death years, ELO rating, Wikipedia URL
- **Work**: Title, author, publication year, form, ELO rating, Wikipedia URL  
- **Comparison**: Tracks voting history for analytics

### Design Principles

- **Responsive Design**: Mobile-first CSS with EB Garamond typography
- **Performance**: Strategic database indexing and optimized queries
- **Security**: Production-ready settings with environment-based configuration
- **Maintainability**: Centralized constants, custom managers, and clean separation of concerns

## License

This project is licensed under the MIT License - see the LICENSE file for details.
