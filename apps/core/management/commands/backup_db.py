import os
import subprocess
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Backup PostgreSQL database using pg_dump and upload via rclone"

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep-local",
            action="store_true",
            help="Keep the local backup file after upload",
        )
        parser.add_argument(
            "--no-upload",
            action="store_true",
            help="Create backup file locally without uploading",
        )
        parser.add_argument(
            "--remote",
            default=None,
            help="rclone remote:path (e.g. gdrive:backups/orbit). Overrides RCLONE_REMOTE setting.",
        )

    def handle(self, *args, **options):
        keep_local = options["keep_local"]
        no_upload = options["no_upload"]
        remote = options["remote"] or settings.RCLONE_REMOTE

        db_settings = settings.DATABASES["default"]
        backup_dir = Path(settings.DB_BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"orbit_backup_{timestamp}.sql.gz"
        filepath = backup_dir / filename

        self.stdout.write(f"Creating database backup: {filename} ...")

        try:
            dump_env = os.environ.copy()
            dump_env["PGPASSWORD"] = db_settings["PASSWORD"]

            result = subprocess.run(
                [
                    "pg_dump",
                    "--host", db_settings["HOST"],
                    "--port", str(db_settings["PORT"]),
                    "--username", db_settings["USER"],
                    "--dbname", db_settings["NAME"],
                    "--no-owner",
                    "--compress", "9",
                    "--file", str(filepath),
                ],
                env=dump_env,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode != 0:
                self.stderr.write(self.style.ERROR(f"pg_dump failed: {result.stderr}"))
                return

            file_size = filepath.stat().st_size
            self.stdout.write(self.style.SUCCESS(
                f"Backup created: {filename} ({file_size / 1024 / 1024:.2f} MB)"
            ))

        except subprocess.TimeoutExpired:
            self.stderr.write(self.style.ERROR("pg_dump timed out after 5 minutes"))
            return
        except FileNotFoundError:
            self.stderr.write(self.style.ERROR(
                "pg_dump not found. Install PostgreSQL client tools."
            ))
            return
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Backup failed: {e}"))
            return

        if not no_upload:
            self.upload_with_rclone(filepath, remote)

        if not keep_local and not no_upload:
            filepath.unlink()
            self.stdout.write(f"Local backup file removed: {filename}")
        elif keep_local:
            self.stdout.write(f"Local backup kept at: {filepath}")

    def upload_with_rclone(self, filepath, remote):
        if not remote:
            self.stderr.write(self.style.ERROR(
                "No rclone remote configured. Set RCLONE_REMOTE in .env or pass --remote."
            ))
            return

        self.stdout.write(f"Uploading to rclone remote: {remote} ...")

        try:
            result = subprocess.run(
                ["rclone", "copy", str(filepath), remote],
                capture_output=True,
                text=True,
                timeout=600,
            )

            if result.returncode != 0:
                self.stderr.write(self.style.ERROR(f"rclone upload failed: {result.stderr}"))
                return

            self.stdout.write(self.style.SUCCESS("Upload complete"))
            self.cleanup_old_rclone_backups(remote)

        except FileNotFoundError:
            self.stderr.write(self.style.ERROR(
                "rclone not found. Install rclone from https://rclone.org"
            ))
        except subprocess.TimeoutExpired:
            self.stderr.write(self.style.ERROR("rclone upload timed out after 10 minutes"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Upload failed: {e}"))

    def cleanup_old_rclone_backups(self, remote):
        max_backups = settings.DB_BACKUP_MAX_KEEP
        if max_backups <= 0:
            return

        self.stdout.write("Cleaning up old backups ...")

        try:
            parent = str(Path(remote).parent)
            result = subprocess.run(
                ["rclone", "lsf", remote, "--format", "tp"],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode != 0:
                self.stderr.write(self.style.WARNING(f"rclone lsf failed: {result.stderr}"))
                return

            files = []
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.strip().split(";", 1)
                if len(parts) == 2:
                    mod_time, name = parts
                    if "orbit_backup_" in name:
                        files.append((mod_time, name))

            files.sort(key=lambda x: x[0])

            if len(files) <= max_backups:
                return

            files_to_delete = files[:-max_backups]
            for _, name in files_to_delete:
                subprocess.run(
                    ["rclone", "delete", f"{remote}/{name}"],
                    capture_output=True,
                    timeout=30,
                )
                self.stdout.write(f"Removed old backup: {name}")

        except Exception as e:
            self.stderr.write(self.style.WARNING(f"Cleanup failed: {e}"))
