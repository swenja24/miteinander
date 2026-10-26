import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import ManagedFile


logger = logging.getLogger(__name__)


@receiver(post_delete, sender=ManagedFile)
def delete_managed_blob(sender, instance, **kwargs):
    """Django intentionally keeps files after model deletion; Miteinander does not."""
    if instance.file:
        storage = instance.file.storage
        name = instance.file.name

        def remove_after_commit():
            try:
                storage.delete(name)
            except Exception:
                logger.exception("Managed file %s could not be removed from storage", name)

        transaction.on_commit(remove_after_commit)
