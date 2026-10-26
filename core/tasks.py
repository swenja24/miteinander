from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import models
from django.utils import timezone
from datetime import timedelta

from .models import AuditEvent, DeveloperFeedback, Invitation, Membership, User, Workspace


@shared_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def send_workspace_notification(workspace_id, author_id, mentioned_usernames, subject, required_permission=""):
    workspace = Workspace.objects.get(pk=workspace_id)
    recipients = []
    for membership in Membership.objects.filter(workspace=workspace, access_status="active", user__is_active=True, user__access_status="active").select_related("user"):
        user = membership.user
        if user.id == int(author_id) or not user.notification_email or user.notification_preference == "none":
            continue
        if required_permission and not (membership.is_admin or membership.permissions.get(required_permission)):
            continue
        if user.notification_preference == "all" or user.username in mentioned_usernames:
            recipients.append(user.notification_email)
    if recipients:
        body = f'In deinem Bereich „{workspace.name}“ gibt es eine neue Information. Melde dich in Miteinander an, um sie zu lesen.'
        # Jede Nachricht wird einzeln versendet, damit E-Mail-Adressen anderer
        # Mitglieder nicht in der Empfängerliste offengelegt werden.
        for recipient in sorted(set(recipients)):
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [recipient], fail_silently=False)


@shared_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def send_feedback_email(feedback_id):
    item = DeveloperFeedback.objects.get(pk=feedback_id)
    send_mail("Verbesserungswunsch für Miteinander", f"Kategorie: {item.category}\n\n{item.message}", settings.DEFAULT_FROM_EMAIL, [settings.DEVELOPER_CONTACT_EMAIL], fail_silently=False)
    item.email_sent = True
    item.save(update_fields=("email_sent",))


@shared_task
def purge_expired_data():
    expired = Workspace.objects.filter(status="deletion-pending", deletion_after__lte=timezone.localdate())
    workspace_count = expired.count()
    expired.delete()
    audit_count, _ = AuditEvent.objects.filter(created_at__lt=timezone.now() - timedelta(days=settings.AUDIT_RETENTION_DAYS)).delete()
    invitation_count, _ = Invitation.objects.filter(
        created_at__lt=timezone.now() - timedelta(days=90),
    ).filter(
        models.Q(expires_at__lt=timezone.now()) | models.Q(accepted_at__isnull=False) | models.Q(revoked_at__isnull=False)
    ).delete()
    expired_totp = User.objects.filter(
        pending_totp_created_at__lt=timezone.now() - timedelta(minutes=15),
    ).update(pending_totp_secret="", pending_totp_created_at=None)
    return {"workspaces": workspace_count, "auditEvents": audit_count, "invitations": invitation_count, "expiredTotpSetups": expired_totp}
