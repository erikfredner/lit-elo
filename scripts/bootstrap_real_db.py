#!/usr/bin/env python

import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

# from core.models import Author, Work

# This script is intended to be used for bulk-inserting real authors/works
# into the database when switching from SQLite to a real database like PostgreSQL.

# Example usage:
# def load_authors_from_csv(filepath):
#     with open(filepath, 'r') as f:
#         reader = csv.DictReader(f)
#         for row in reader:
#             Author.objects.create(
#                 name=row['name'],
#                 birth_year=row.get('birth_year'),
#                 death_year=row.get('death_year')
#             )

# if __name__ == '__main__':
#     print("Starting database bootstrapping...")
#     # Call your data loading functions here
#     # load_authors_from_csv('path/to/your/authors.csv')
#     print("Database bootstrapping complete.")
