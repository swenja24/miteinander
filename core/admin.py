from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import AuditEvent, DeveloperFeedback, Invitation, ManagedFile, Member, Membership, Record, User, Workspace, WorkspaceOptions, WorkspaceProfile


@admin.register(User)
class MiteinanderUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("Miteinander", {"fields": ("display_name", "access_status", "notification_email", "notification_preference", "is_system_admin", "totp_enabled")}),)
    list_display = ("username", "display_name", "access_status", "is_system_admin", "is_active")


for model in (Workspace, Membership, Member, WorkspaceProfile, WorkspaceOptions, Record, ManagedFile, Invitation, AuditEvent, DeveloperFeedback):
    admin.site.register(model)
