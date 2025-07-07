"""
Management command to clean up old comparison records.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta

from core.models import Comparison
from core.constants import COMPARISON_CLEANUP_DAYS


class Command(BaseCommand):
    help = 'Clean up old comparison records to prevent database bloat'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=COMPARISON_CLEANUP_DAYS,
            help=f'Remove comparison records older than this many days (default: {COMPARISON_CLEANUP_DAYS})',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be deleted without actually deleting'
        )

    def handle(self, *args, **options):
        days = options['days']
        dry_run = options['dry_run']
        
        # Count records before cleanup
        total_before = Comparison.objects.count()
        
        cutoff = timezone.now() - timedelta(days=days)
        old_comparisons = Comparison.objects.filter(created_at__lt=cutoff)
        count_to_delete = old_comparisons.count()
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f'DRY RUN: Would delete {count_to_delete} comparison records older than {days} days. '
                    f'Total records: {total_before}'
                )
            )
        else:
            # Clean up old records
            Comparison.cleanup_old_comparisons(days=days)
            
            # Count records after cleanup
            total_after = Comparison.objects.count()
            deleted = total_before - total_after
            
            self.stdout.write(
                self.style.SUCCESS(
                    f'Successfully cleaned up {deleted} comparison records older than {days} days. '
                    f'Remaining records: {total_after}'
                )
            )
