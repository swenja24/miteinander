import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q


def default_permissions():
    return {key: False for key in ("cases", "tasks", "calendar", "documents", "ledger", "family")}


class User(AbstractUser):
    display_name = models.CharField(max_length=200, blank=True)
    access_status = models.CharField(max_length=20, default="active")
    notification_email = models.EmailField(blank=True)
    notification_preference = models.CharField(max_length=20, default="mentions")
    is_system_admin = models.BooleanField(default=False)
    totp_secret = models.CharField(max_length=128, blank=True)
    pending_totp_secret = models.CharField(max_length=128, blank=True)
    totp_enabled = models.BooleanField(default=False)
    legacy_password_hash = models.TextField(blank=True)
    onboarding_dismissed = models.JSONField(default=list, blank=True)

    @property
    def label(self):
        return self.display_name or self.get_full_name() or self.username


class Workspace(models.Model):
    class Type(models.TextChoices):
        PRIVATE = "private", "Persönlich"
        SHARED = "shared", "Gemeinsam"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=250)
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.PRIVATE)
    person_name = models.CharField(max_length=250, blank=True)
    status = models.CharField(max_length=30, default="active")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_workspaces")
    deletion_requested_at = models.DateTimeField(null=True, blank=True)
    deletion_after = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.name


class ManagedFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="files")
    category = models.CharField(max_length=40)
    file = models.FileField(upload_to="uploads/%Y/%m")
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)


class Member(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    home_workspace = models.ForeignKey(Workspace, null=True, blank=True, on_delete=models.CASCADE, related_name="home_members")
    user = models.OneToOneField(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="member_profile")
    name = models.CharField(max_length=250)
    role = models.CharField(max_length=100, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=100, blank=True)
    address = models.TextField(blank=True)
    personal_words = models.TextField(blank=True)
    color = models.CharField(max_length=30, default="#285c4d")
    photo = models.ForeignKey(ManagedFile, null=True, blank=True, on_delete=models.SET_NULL, related_name="member_photos")
    created_at = models.DateTimeField(auto_now_add=True)


class Membership(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="workspace_memberships")
    member = models.ForeignKey(Member, null=True, blank=True, on_delete=models.SET_NULL, related_name="memberships")
    role = models.CharField(max_length=100, blank=True)
    is_admin = models.BooleanField(default=False)
    access_status = models.CharField(max_length=20, default="active")
    permissions = models.JSONField(default=default_permissions)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("workspace", "user"), name="unique_workspace_user")]
        indexes = [models.Index(fields=("workspace", "is_admin"))]


class WorkspaceProfile(models.Model):
    workspace = models.OneToOneField(Workspace, primary_key=True, on_delete=models.CASCADE, related_name="profile")
    introduction = models.TextField(blank=True)
    strengths = models.TextField(blank=True)
    support_needs = models.TextField(blank=True)
    bei_summary = models.TextField(blank=True)
    wishes = models.TextField(blank=True)
    photo = models.ForeignKey(ManagedFile, null=True, blank=True, on_delete=models.SET_NULL, related_name="workspace_photos")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)


def default_options():
    return {
        "contact": {"categories": ["Eltern / Familie", "Fahrdienst / Busunternehmen", "WfB / Arbeit", "Ärzt*innen", "Therapie", "Pflege", "Behörde", "Wohnen", "Notfallkontakt", "Sonstiges"]},
        "ledger": {"descriptions": [], "categories": []},
        "task": {"categories": []},
    }


class WorkspaceOptions(models.Model):
    workspace = models.OneToOneField(Workspace, primary_key=True, on_delete=models.CASCADE, related_name="options")
    value = models.JSONField(default=default_options)


class Record(models.Model):
    """A tenant-scoped domain record with indexed operational fields.

    Each item is a separate PostgreSQL row. The JSON payload holds fields that
    differ between social-work modules, while frequently queried fields remain
    typed and indexed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="records")
    kind = models.CharField(max_length=40)
    title = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=50, blank=True)
    event_date = models.DateField(null=True, blank=True)
    parent_id = models.UUIDField(null=True, blank=True)
    series_id = models.UUIDField(null=True, blank=True)
    account_id = models.UUIDField(null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    payload = models.JSONField(default=dict)
    attachment = models.ForeignKey(ManagedFile, null=True, blank=True, on_delete=models.SET_NULL, related_name="records")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_records")
    created_by_name = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=("workspace", "kind", "event_date")),
            models.Index(fields=("workspace", "kind", "status")),
            models.Index(fields=("workspace", "series_id")),
            models.Index(fields=("workspace", "account_id")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("workspace", "kind", "series_id", "event_date"),
                condition=Q(series_id__isnull=False),
                name="unique_series_occurrence",
            ),
        ]


class Invitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, null=True, blank=True, on_delete=models.CASCADE, related_name="invitations")
    token_hash = models.CharField(max_length=64, unique=True)
    display_name = models.CharField(max_length=250, blank=True)
    email = models.EmailField(blank=True)
    role = models.CharField(max_length=100, blank=True)
    permissions = models.JSONField(default=default_permissions)
    create_private_area = models.BooleanField(default=False)
    area_name = models.CharField(max_length=250, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="created_invitations")
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="accepted_invitations")
    revoked_at = models.DateTimeField(null=True, blank=True)


class CalendarFeed(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="calendar_feeds")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="calendar_feeds")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("workspace", "user"), name="unique_workspace_feed")]


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="audit_events")
    action = models.CharField(max_length=80)
    entity = models.CharField(max_length=80)
    entity_id = models.CharField(max_length=100, blank=True)
    summary = models.CharField(max_length=1000, blank=True)
    actor = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    actor_name = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("workspace", "-created_at"))]


class DeveloperFeedback(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.CharField(max_length=100)
    message = models.TextField()
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    created_by_name = models.CharField(max_length=250)
    workspace_type = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=20, default="new")
    email_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
