#!/usr/bin/env python
"""
Generate additional test data for pagination testing.
"""
import os
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from core.models import Author, Work

# Additional authors to add
additional_authors = [
    ("Marcel Proust", 1871, 1922),
    ("Franz Kafka", 1883, 1924),
    ("Jorge Luis Borges", 1899, 1986),
    ("Chinua Achebe", 1930, 2013),
    ("Haruki Murakami", 1949, None),
    ("Elena Ferrante", 1943, None),  # pseudonym, birth year approximate
    ("Octavia Butler", 1947, 2006),
    ("Salman Rushdie", 1947, None),
    ("Maya Angelou", 1928, 2014),
    ("Zora Neale Hurston", 1891, 1960),
    ("Ralph Ellison", 1914, 1994),
    ("Flannery O'Connor", 1925, 1964),
    ("Kurt Vonnegut", 1922, 2007),
    ("Margaret Atwood", 1939, None),
    ("Ursula K. Le Guin", 1929, 2018),
    ("Toni Cade Bambara", 1939, 1995),
    ("Alice Walker", 1944, None),
    ("James Baldwin", 1924, 1987),
    ("Langston Hughes", 1901, 1967),
    ("W.E.B. Du Bois", 1868, 1963),
    ("Zora Neale Hurston", 1891, 1960),
    ("Richard Wright", 1908, 1960),
    ("Claude McKay", 1889, 1948),
    ("Countee Cullen", 1903, 1946),
    ("Jean Toomer", 1894, 1967),
    ("Nella Larsen", 1891, 1964),
    ("Jessie Redmon Fauset", 1882, 1961),
    ("Arna Bontemps", 1902, 1973),
    ("Sterling Brown", 1901, 1989),
    ("Gwendolyn Brooks", 1917, 2000),
    ("Rita Dove", 1952, None),
    ("Yusef Komunyakaa", 1947, None),
    ("Natasha Trethewey", 1966, None),
    ("Tracy K. Smith", 1972, None),
    ("Jericho Brown", 1976, None),
    ("Terrance Hayes", 1971, None),
    ("Claudia Rankine", 1963, None),
    ("Ocean Vuong", 1988, None),
    ("Danez Smith", 1989, None),
    ("Saeed Jones", 1985, None),
]

# Additional works with their authors
additional_works = [
    ("In Search of Lost Time", "Marcel Proust"),
    ("The Metamorphosis", "Franz Kafka"),
    ("The Trial", "Franz Kafka"),
    ("Labyrinths", "Jorge Luis Borges"),
    ("Things Fall Apart", "Chinua Achebe"),
    ("Norwegian Wood", "Haruki Murakami"),
    ("My Brilliant Friend", "Elena Ferrante"),
    ("Kindred", "Octavia Butler"),
    ("Midnight's Children", "Salman Rushdie"),
    ("I Know Why the Caged Bird Sings", "Maya Angelou"),
    ("Their Eyes Were Watching God", "Zora Neale Hurston"),
    ("Invisible Man", "Ralph Ellison"),
    ("A Good Man Is Hard to Find", "Flannery O'Connor"),
    ("Slaughterhouse-Five", "Kurt Vonnegut"),
    ("The Handmaid's Tale", "Margaret Atwood"),
    ("The Left Hand of Darkness", "Ursula K. Le Guin"),
    ("The Salt Eaters", "Toni Cade Bambara"),
    ("The Color Purple", "Alice Walker"),
    ("Giovanni's Room", "James Baldwin"),
    ("The Weary Blues", "Langston Hughes"),
    ("The Souls of Black Folk", "W.E.B. Du Bois"),
    ("Native Son", "Richard Wright"),
    ("Home to Harlem", "Claude McKay"),
    ("Color", "Countee Cullen"),
    ("Cane", "Jean Toomer"),
    ("Passing", "Nella Larsen"),
    ("There Is Confusion", "Jessie Redmon Fauset"),
    ("Black Thunder", "Arna Bontemps"),
    ("Southern Road", "Sterling Brown"),
    ("Annie Allen", "Gwendolyn Brooks"),
    ("Thomas and Beulah", "Rita Dove"),
    ("Dien Cai Dau", "Yusef Komunyakaa"),
    ("Native Guard", "Natasha Trethewey"),
    ("Life on Mars", "Tracy K. Smith"),
    ("The New Testament", "Jericho Brown"),
    ("Lighthead", "Terrance Hayes"),
    ("Citizen", "Claudia Rankine"),
    ("Night Sky with Exit Wounds", "Ocean Vuong"),
    ("Don't Call Us Dead", "Danez Smith"),
    ("How We Fight for Our Lives", "Saeed Jones"),
]

# Create authors
for name, birth_year, death_year in additional_authors:
    author, created = Author.objects.get_or_create(
        name=name,
        defaults={'birth_year': birth_year, 'death_year': death_year}
    )
    if created:
        print(f"Created author: {name}")
    else:
        print(f"Author already exists: {name}")

# Create works
for title, author_name in additional_works:
    try:
        author = Author.objects.get(name=author_name)
        work, created = Work.objects.get_or_create(
            title=title,
            author=author
        )
        if created:
            print(f"Created work: {title} by {author_name}")
        else:
            print(f"Work already exists: {title}")
    except Author.DoesNotExist:
        print(f"Author not found: {author_name} for work {title}")

print(f"\nTotal authors: {Author.objects.count()}")
print(f"Total works: {Work.objects.count()}")
