# Generated manually for Trustpilot Multilogin profile mapping.

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="TrustpilotAccount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254, unique=True)),
                ("external_id", models.CharField(blank=True, max_length=255, null=True, unique=True)),
                ("is_authorized", models.BooleanField(default=True)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Trustpilot Account",
                "verbose_name_plural": "Trustpilot Accounts",
                "db_table": "trustpilot_account",
            },
        ),
        migrations.CreateModel(
            name="TrustpilotMultiloginProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("multilogin_profile_id", models.CharField(max_length=255, unique=True)),
                ("multilogin_profile_name", models.CharField(max_length=255)),
                ("multilogin_folder_id", models.CharField(max_length=255)),
                ("browser_type", models.CharField(default="mimic", max_length=32)),
                ("os_type", models.CharField(default="windows", max_length=32)),
                (
                    "status",
                    models.CharField(
                        choices=[("created", "Created"), ("bound", "Bound"), ("failed", "Failed")],
                        default="bound",
                        max_length=32,
                    ),
                ),
                ("api_response", models.JSONField(blank=True, default=dict)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "trustpilot_account",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="multilogin_profile",
                        to="trustpilot_integrations.trustpilotaccount",
                    ),
                ),
            ],
            options={
                "verbose_name": "Trustpilot Multilogin Profile",
                "verbose_name_plural": "Trustpilot Multilogin Profiles",
                "db_table": "trustpilot_multilogin_profile",
            },
        ),
    ]
