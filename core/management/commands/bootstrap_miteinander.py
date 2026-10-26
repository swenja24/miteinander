import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Member, Workspace
from core.services import create_workspace


class Command(BaseCommand):
    help = "Legt bei einer leeren Installation den ersten Betreiberzugang an."

    def handle(self, *args, **options):
        User = get_user_model()
        if User.objects.exists() or Workspace.objects.exists():
            self.stdout.write("Bootstrap übersprungen: Die Installation enthält bereits Daten.")
            return
        username = os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "admin").strip().lower()
        password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
        display_name = os.environ.get("BOOTSTRAP_ADMIN_NAME", "Administration").strip()
        person_name = os.environ.get("BOOTSTRAP_PERSON_NAME", display_name).strip()
        area_name = os.environ.get("BOOTSTRAP_WORKSPACE_NAME", f"{person_name}s Bereich").strip()
        admin_role = os.environ.get("BOOTSTRAP_ADMIN_ROLE", "Gesetzliche Betreuung").strip()[:100]
        if len(username) < 3 or len(username) > 150:
            raise CommandError("BOOTSTRAP_ADMIN_USERNAME muss zwischen 3 und 150 Zeichen lang sein.")
        if not display_name or not person_name or not area_name:
            raise CommandError("Name, leistungsberechtigte Person und Bereichsname dürfen nicht leer sein.")
        if not password:
            raise CommandError("BOOTSTRAP_ADMIN_PASSWORD fehlt. Der erste Zugang wurde nicht angelegt.")
        try:
            validate_password(password)
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from error
        with transaction.atomic():
            user = User.objects.create_user(username=username, password=password, display_name=display_name, is_system_admin=True)
            workspace, membership = create_workspace(user, area_name, Workspace.Type.PRIVATE, person_name)
            if display_name.casefold() != person_name.casefold() or admin_role != "Leistungsberechtigte Person":
                person = membership.member
                person.user = None
                person.save(update_fields=("user",))
                administrator = Member.objects.create(home_workspace=workspace, user=user, name=display_name, role=admin_role, email=user.notification_email, color="#285c4d")
                membership.member = administrator
                membership.role = admin_role
                membership.save(update_fields=("member", "role"))
        self.stdout.write(self.style.SUCCESS(f"Erster Zugang „{username}“ und Bereich „{area_name}“ angelegt."))
