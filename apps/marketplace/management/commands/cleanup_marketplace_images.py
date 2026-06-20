import os

from apps.marketplace.models import ProductImage
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Clean up orphaned marketplace images and report storage usage'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Only report what would be deleted, without deleting',
        )
        parser.add_argument(
            '--delete-orphans',
            action='store_true',
            help='Actually delete orphaned files (default: dry-run only)',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', True) or not options.get('delete_orphans')

        # Collect all DB-referenced image paths
        db_paths = set()
        for pi in ProductImage.objects.iterator():
            if pi.image:
                db_paths.add(pi.image.name)
                # Also check for .jpg variant (from loop bug)
                base, ext = os.path.splitext(pi.image.name)
                if ext.lower() in ('.png', '.webp'):
                    jpg_path = f"{base}.jpg"
                    db_paths.add(jpg_path)

        # List all files in marketplace/products/ storage
        storage_dir = 'marketplace/products/'
        orphan_count = 0
        orphan_size = 0
        total_files = 0
        total_size = 0

        try:
            dirs, files = default_storage.listdir(storage_dir)
            for fname in files:
                file_path = os.path.join(storage_dir, fname)
                try:
                    fsize = default_storage.size(file_path)
                except Exception:
                    fsize = 0
                total_files += 1
                total_size += fsize

                if file_path not in db_paths:
                    orphan_count += 1
                    orphan_size += fsize
                    if not dry_run:
                        default_storage.delete(file_path)
                        self.stdout.write(f"  Deleted orphan: {file_path} ({fsize / 1024 / 1024:.1f}MB)")
                    else:
                        self.stdout.write(f"  Orphan: {file_path} ({fsize / 1024 / 1024:.1f}MB)")
        except FileNotFoundError:
            self.stdout.write(self.style.WARNING(f"Storage directory '{storage_dir}' not found."))
            return

        self.stdout.write()
        self.stdout.write(f"Total files in {storage_dir}: {total_files}")
        self.stdout.write(f"Total storage: {total_size / 1024 / 1024:.1f}MB")
        self.stdout.write(f"Orphan files: {orphan_count} ({orphan_size / 1024 / 1024:.1f}MB)")
        self.stdout.write(f"DB-referenced files: {len(db_paths)}")

        if dry_run and orphan_count > 0:
            self.stdout.write(self.style.WARNING(
                f"\nRun with --delete-orphans to remove {orphan_count} orphan files "
                f"and free {orphan_size / 1024 / 1024:.1f}MB."
            ))
        elif orphan_count == 0:
            self.stdout.write(self.style.SUCCESS("No orphan files found."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Cleaned up {orphan_count} orphan files."))
