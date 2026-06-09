from django.db import models
from django.utils import timezone


class TrustpilotAccount(models.Model):
    """
    Minimal account model for authorized Trustpilot accounts.

    If the main project already has a Trustpilot account model, replace references to this
    model in the management command with the existing model instead of creating duplicates.
    """

    email = models.EmailField(unique=True)
    external_id = models.CharField(max_length=255, unique=True, blank=True, null=True)
    is_authorized = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "trustpilot_account"
        verbose_name = "Trustpilot Account"
        verbose_name_plural = "Trustpilot Accounts"

    def __str__(self) -> str:
        return self.email


class TrustpilotMultiloginProfile(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "Created"
        BOUND = "bound", "Bound"
        FAILED = "failed", "Failed"

    trustpilot_account = models.OneToOneField(
        TrustpilotAccount,
        on_delete=models.CASCADE,
        related_name="multilogin_profile",
    )
    multilogin_profile_id = models.CharField(max_length=255, unique=True)
    multilogin_profile_name = models.CharField(max_length=255)
    multilogin_folder_id = models.CharField(max_length=255)
    browser_type = models.CharField(max_length=32, default="mimic")
    os_type = models.CharField(max_length=32, default="windows")
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.BOUND)
    api_response = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "trustpilot_multilogin_profile"
        verbose_name = "Trustpilot Multilogin Profile"
        verbose_name_plural = "Trustpilot Multilogin Profiles"

    def __str__(self) -> str:
        return f"{self.trustpilot_account.email} -> {self.multilogin_profile_id}"
