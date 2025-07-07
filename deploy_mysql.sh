#!/bin/bash
# MySQL Deployment Setup Script for Canon Wars

set -e  # Exit on any error

echo "🚀 Canon Wars MySQL Deployment Setup"
echo "======================================"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

# Check if .env file exists
if [ ! -f ".env" ]; then
    print_warning "No .env file found. Creating from template..."
    cp .env.example .env
    print_warning "Please edit .env file with your database credentials before continuing."
    exit 1
fi

# Source environment variables
export $(grep -v '^#' .env | xargs)

print_status "Environment variables loaded"

# Check if MySQL client is available
if ! command -v mysql &> /dev/null; then
    print_error "MySQL client not found. Please install MySQL client."
    exit 1
fi

# Install Python dependencies
print_status "Installing Python dependencies..."
pip install -e .

# Test database connection
print_status "Testing database connection..."
python manage.py dbshell --command="SELECT 1;" 2>/dev/null || {
    print_error "Cannot connect to database. Please check your .env configuration."
    exit 1
}

print_status "Database connection successful"

# Run migrations
print_status "Running database migrations..."
python manage.py migrate

# Create superuser if it doesn't exist
print_status "Setting up admin user..."
python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username='admin').exists():
    User.objects.create_superuser('admin', 'admin@example.com', 'admin123')
    print('Admin user created: admin/admin123')
else:
    print('Admin user already exists')
"

# Load fixtures if available
if [ -f "fixtures/authors.json" ] && [ -f "fixtures/works.json" ]; then
    print_status "Loading initial data..."
    python manage.py loaddata fixtures/authors.json fixtures/works.json
else
    print_warning "No fixtures found. You may want to load initial data manually."
fi

# Collect static files
print_status "Collecting static files..."
python manage.py collectstatic --noinput

# Run basic tests
print_status "Running tests..."
python manage.py test core.test_refactored --verbosity=1

print_status "✨ Deployment setup complete!"
echo ""
echo "Next steps:"
echo "1. Update ALLOWED_HOSTS in production settings"
echo "2. Set up your web server (nginx/Apache)"
echo "3. Configure HTTPS"
echo "4. Set up monitoring and backups"
echo ""
echo "Admin panel: /admin/"
echo "Default admin user: admin / admin123"
echo "🎉 Canon Wars is ready to go!"
