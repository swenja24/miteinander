from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import AuditEvent, Workspace


class Command(BaseCommand):
    help = "Löscht abgelaufene Löschaufträge und alte Änderungsprotokolle."

    def handle(self, *args, **options):
        today = timezone.localdate()
        workspaces = Workspace.objects.filter(status="deletion-pending", deletion_after__lte=today)
        deleted_workspaces = workspaces.count()
        workspaces.delete()
        cutoff = timezone.now() - timedelta(days=settings.AUDIT_RETENTION_DAYS)
        deleted_audits, _ = AuditEvent.objects.filter(created_at__lt=cutoff).delete()
        self.stdout.write(f"{deleted_workspaces} Bereiche und {deleted_audits} alte Protokolleinträge gelöscht.")
