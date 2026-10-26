from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import ManagedFile


@receiver(post_delete, sender=ManagedFile)
def delete_managed_blob(sender, instance, **kwargs):
    """Django intentionally keeps files after model deletion; Miteinander does not."""
    if instance.file:
        instance.file.delete(save=False)
