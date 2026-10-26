from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0003_membership_access_status")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="pending_totp_created_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
