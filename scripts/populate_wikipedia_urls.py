#!/usr/bin/env python
"""Script to populate correct Wikipedia URLs for some authors and works"""

import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from core.models import Author, Work

def populate_author_urls():
    """Add correct Wikipedia URLs for authors"""
    author_urls = {
        "William Shakespeare": "https://en.wikipedia.org/wiki/William_Shakespeare",
        "Toni Morrison": "https://en.wikipedia.org/wiki/Toni_Morrison",
        "Jane Austen": "https://en.wikipedia.org/wiki/Jane_Austen",
        "Leo Tolstoy": "https://en.wikipedia.org/wiki/Leo_Tolstoy",
        "Virginia Woolf": "https://en.wikipedia.org/wiki/Virginia_Woolf",
        "Gabriel García Márquez": "https://en.wikipedia.org/wiki/Gabriel_Garc%C3%ADa_M%C3%A1rquez",
        "James Joyce": "https://en.wikipedia.org/wiki/James_Joyce",
        "Emily Dickinson": "https://en.wikipedia.org/wiki/Emily_Dickinson",
        "Charles Dickens": "https://en.wikipedia.org/wiki/Charles_Dickens",
        "Maya Angelou": "https://en.wikipedia.org/wiki/Maya_Angelou",
        "Franz Kafka": "https://en.wikipedia.org/wiki/Franz_Kafka",
        "Zora Neale Hurston": "https://en.wikipedia.org/wiki/Zora_Neale_Hurston",
    }
    
    for name, url in author_urls.items():
        try:
            author = Author.objects.get(name=name)
            author.wikipedia_url = url
            author.save()
            print(f"✓ Updated {name}")
        except Author.DoesNotExist:
            print(f"✗ Author '{name}' not found")

def populate_work_urls():
    """Add correct Wikipedia URLs for works"""
    work_urls = {
        ("Hamlet", "William Shakespeare"): "https://en.wikipedia.org/wiki/Hamlet",
        ("Beloved", "Toni Morrison"): "https://en.wikipedia.org/wiki/Beloved_(novel)",
        ("Pride and Prejudice", "Jane Austen"): "https://en.wikipedia.org/wiki/Pride_and_Prejudice",
        ("War and Peace", "Leo Tolstoy"): "https://en.wikipedia.org/wiki/War_and_Peace",
        ("Mrs Dalloway", "Virginia Woolf"): "https://en.wikipedia.org/wiki/Mrs_Dalloway",
        ("One Hundred Years of Solitude", "Gabriel García Márquez"): "https://en.wikipedia.org/wiki/One_Hundred_Years_of_Solitude",
        ("Ulysses", "James Joyce"): "https://en.wikipedia.org/wiki/Ulysses_(novel)",
        ("Because I could not stop for Death", "Emily Dickinson"): "https://en.wikipedia.org/wiki/Because_I_could_not_stop_for_Death",
        ("Great Expectations", "Charles Dickens"): "https://en.wikipedia.org/wiki/Great_Expectations",
        ("I Know Why the Caged Bird Sings", "Maya Angelou"): "https://en.wikipedia.org/wiki/I_Know_Why_the_Caged_Bird_Sings",
        ("The Metamorphosis", "Franz Kafka"): "https://en.wikipedia.org/wiki/The_Metamorphosis",
        ("Their Eyes Were Watching God", "Zora Neale Hurston"): "https://en.wikipedia.org/wiki/Their_Eyes_Were_Watching_God",
        ("Macbeth", "William Shakespeare"): "https://en.wikipedia.org/wiki/Macbeth",
        ("Romeo and Juliet", "William Shakespeare"): "https://en.wikipedia.org/wiki/Romeo_and_Juliet",
        ("Anna Karenina", "Leo Tolstoy"): "https://en.wikipedia.org/wiki/Anna_Karenina",
        ("To the Lighthouse", "Virginia Woolf"): "https://en.wikipedia.org/wiki/To_the_Lighthouse",
        ("A Portrait of the Artist as a Young Man", "James Joyce"): "https://en.wikipedia.org/wiki/A_Portrait_of_the_Artist_as_a_Young_Man",
        ("The Song of Solomon", "Toni Morrison"): "https://en.wikipedia.org/wiki/Song_of_Solomon_(novel)",
    }
    
    for (title, author_name), url in work_urls.items():
        try:
            author = Author.objects.get(name=author_name)
            work = Work.objects.get(title=title, author=author)
            work.wikipedia_url = url
            work.save()
            print(f"✓ Updated '{title}' by {author_name}")
        except (Author.DoesNotExist, Work.DoesNotExist):
            print(f"✗ Work '{title}' by '{author_name}' not found")

if __name__ == "__main__":
    print("=== Populating Author Wikipedia URLs ===")
    populate_author_urls()
    print("\n=== Populating Work Wikipedia URLs ===")
    populate_work_urls()
    print("\n✅ Done! All Wikipedia URLs have been updated.")
    print("You can now edit these URLs in the Django admin if needed.")
