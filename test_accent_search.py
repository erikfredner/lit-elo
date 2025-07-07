#!/usr/bin/env python
"""Test script for accent-insensitive search functionality"""

import os
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from core.models import Author, Work
from core.managers import normalize_search_text

# Test the normalization function
print("Testing text normalization:")
test_cases = [
    ("márquez", "marquez"),
    ("García", "garcia"),
    ("José", "jose"),
    ("François", "francois"),
    ("Müller", "muller"),
    ("naïve", "naive"),
]

for original, expected in test_cases:
    normalized = normalize_search_text(original)
    print(f"'{original}' -> '{normalized}' (expected: '{expected}')")
    assert normalized == expected, f"Expected '{expected}', got '{normalized}'"

print("\nAll normalization tests passed!")

# Create test authors with accented names
print("\nCreating test authors...")
test_authors = [
    "Gabriel García Márquez",
    "José Saramago", 
    "François Mauriac",
    "Günter Grass",
    "Björk Guðmundsdóttir",  # OK, not an author but good for testing!
]

# Clean up any existing test authors
Author.objects.filter(name__in=test_authors).delete()

# Create new test authors
for name in test_authors:
    Author.objects.create(name=name)
    print(f"Created author: {name}")

print("\nTesting search functionality...")

# Test searches
search_tests = [
    ("marquez", "Gabriel García Márquez"),
    ("garcia", "Gabriel García Márquez"),
    ("jose", "José Saramago"),
    ("francois", "François Mauriac"),
    ("gunter", "Günter Grass"),
    ("bjork", "Björk Guðmundsdóttir"),
]

for search_term, expected_name in search_tests:
    print(f"\nSearching for '{search_term}':")
    results = Author.objects.search(search_term)
    found_names = [author.name for author in results]
    print(f"Found: {found_names}")
    
    if expected_name in found_names:
        print(f"✓ SUCCESS: Found '{expected_name}'")
    else:
        print(f"✗ FAILURE: Expected to find '{expected_name}'")

print("\nTest completed!")
