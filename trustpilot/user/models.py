from django.db import models
from django.utils import timezone


class User(models.Model):
    name = models.CharField(max_length=255, null=True, blank=True)
    email = models.EmailField(unique=True)
    password = models.CharField(max_length=255)
    date_created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name or self.email


class Profile(models.Model):
    class StageStatus(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        PENDING = "pending", "Pending"
        IN_PROGRESS = "in_progress", "In progress"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        RETRY_REQUIRED = "retry_required", "Retry required"
        PAUSED = "paused", "Paused"
        COMPLETED = "completed", "Completed"

    # Identity fields
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="profiles")
    profile_name = models.CharField(max_length=255)
    email = models.EmailField(null=True, blank=True)
    password = models.CharField(max_length=255, null=True, blank=True)
    fa2_secret = models.CharField(max_length=255, null=True, blank=True)
    ml_profile_id = models.CharField(max_length=255)
    ml_folder_id = models.CharField(max_length=255)
    date_created = models.DateTimeField(default=timezone.now, null=True, blank=True)

    # Daily progress tracking
    current_day = models.IntegerField(default=1, null=True, blank=True)
    current_stage = models.CharField(max_length=50, null=True, blank=True)
    stage_status = models.CharField(
        max_length=20,
        choices=StageStatus.choices,
        default=StageStatus.NOT_STARTED,
    )
    last_completed_day = models.IntegerField(null=True, blank=True)
    next_action_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(null=True, blank=True)
    total_successful_days = models.IntegerField(default=0, null=True, blank=True)
    total_failed_days = models.IntegerField(default=0, null=True, blank=True)

    # Reporting fields
    reviews_count = models.IntegerField(default=0, null=True, blank=True)
    interactions_count = models.IntegerField(default=0, null=True, blank=True)
    total_duration = models.DurationField(null=True, blank=True)

    class Meta:
        verbose_name = "Profile"
        verbose_name_plural = "Profiles"

    @property
    def warm_duration(self):
        if self.total_duration:
            return str(self.total_duration).split('.')[0]
        return "0:00:00"

    def __str__(self):
        return f"{self.profile_name} ({self.user.email})"


class UserTaskManager(models.Model):
    class Status(models.TextChoices):
        ON_HOLD = "on_hold", "On hold"
        PENDING = "pending", "Pending"
        IN_PROGRESS = "in_progress", "In progress"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    profile = models.ForeignKey(Profile, on_delete=models.CASCADE, related_name="tasks", null=True, blank=True)
    action = models.CharField(max_length=255)
    task_status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    started_at = models.DateTimeField(default=timezone.now, null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration = models.DurationField(null=True, blank=True)
    interactions_count = models.IntegerField(default=0, null=True, blank=True)
    comment = models.TextField(null=True, blank=True)
    execute_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    # Task execution parameters
    pause_multiplier = models.FloatField(default=1.0, null=True, blank=True)
    interactions_count_from = models.IntegerField(default=0, null=True, blank=True)
    interactions_count_to = models.IntegerField(default=0, null=True, blank=True)
    company_queries_count_from = models.IntegerField(default=3, null=True, blank=True)
    company_queries_count_to = models.IntegerField(default=5, null=True, blank=True)
    company_queries = models.JSONField(default=list, null=True, blank=True)
    restricted_company_queries = models.JSONField(default=list, null=True, blank=True)
    target_company = models.CharField(max_length=255, null=True, blank=True)
    review_length = models.IntegerField(null=True, blank=True)
    prompt_parameters = models.CharField(max_length=255, null=True, blank=True)
    prompts = models.JSONField(default=list, null=True, blank=True)
    limit_on_reviews_duration = models.IntegerField(null=True, blank=True)
    maximum_longer_reviews = models.IntegerField(null=True, blank=True)
    maximum_review_pages = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-execute_at"]
        verbose_name = "User task"
        verbose_name_plural = "User tasks"

    def save(self, *args, **kwargs):
        if self.started_at and self.finished_at:
            self.duration = self.finished_at - self.started_at
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.profile.profile_name} - {self.action} ({self.task_status})"


class UserTaskLog(models.Model):
    class LogLevel(models.TextChoices):
        INFO = "info", "INFO"
        WARNING = "warning", "WARNING"
        ERROR = "error", "ERROR"
        DEBUG = "debug", "DEBUG"

    task = models.ForeignKey(UserTaskManager, on_delete=models.CASCADE, related_name="logs")
    level = models.CharField(max_length=20, choices=LogLevel.choices, default=LogLevel.INFO)
    message = models.TextField()
    comment = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Task log"
        verbose_name_plural = "Task logs"

    def __str__(self):
        return f"[{self.get_level_display()}] {self.task.action} - {self.created_at}: {self.message}"


class ProfileDayProgress(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_PROGRESS = "in_progress", "In progress"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"
        RETRY_REQUIRED = "retry_required", "Retry required"

    profile = models.ForeignKey(Profile, on_delete=models.CASCADE, related_name="day_progress")
    day_number = models.IntegerField()
    scenario_name = models.CharField(max_length=50)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.IntegerField(null=True, blank=True)
    task = models.ForeignKey(
        UserTaskManager,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="day_progress",
    )
    error_message = models.TextField(null=True, blank=True)
    retry_count = models.IntegerField(default=0)
    interactions_count = models.IntegerField(default=0, null=True, blank=True)
    reviews_count = models.IntegerField(default=0, null=True, blank=True)
    metadata = models.JSONField(default=dict, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Profile day progress"
        verbose_name_plural = "Profile day progress"
        indexes = [
            models.Index(fields=["profile", "day_number"]),
            models.Index(fields=["status"]),
            models.Index(fields=["scenario_name"]),
        ]

    def __str__(self):
        return f"{self.profile.profile_name} - Day {self.day_number} ({self.status})"


class Review(models.Model):
    class Status(models.TextChoices):
        SUCCESSFUL = 'successful', 'Successful'
        UNSUCCESSFUL = 'unsuccessful', 'Unsuccessful'

    profile = models.ForeignKey(Profile, on_delete=models.CASCADE, related_name='reviews')
    task = models.ForeignKey(
        UserTaskManager, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviews'
    )
    company = models.CharField(max_length=255)
    review_title = models.CharField(max_length=500, null=True, blank=True)
    review_text = models.TextField(null=True, blank=True)
    rating = models.IntegerField(null=True, blank=True)
    review_link = models.URLField(max_length=512, null=True, blank=True)
    trustpilot_username = models.CharField(max_length=255, null=True, blank=True)
    # Null means "not yet classified" — automation always sets this explicitly,
    # manual admin entries should choose before saving.
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        null=True,
        blank=True,
        default=None,
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Review'
        verbose_name_plural = 'Reviews'
        indexes = [
            models.Index(fields=['profile']),
            models.Index(fields=['company']),
            models.Index(fields=['status']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.profile.profile_name} → {self.company} ({self.rating}★)"


class ReviewStatisticsProxy(Review):
    """Proxy used exclusively to register the statistics admin page. No DB table."""
    class Meta:
        proxy = True
        verbose_name = 'Reviews Statistics'
        verbose_name_plural = 'Reviews Statistics'


class ProfileSessionEvent(models.Model):
    class SessionStatus(models.TextChoices):
        ALIVE = "alive", "Alive"
        EXPIRED = "expired", "Expired"
        RELOGIN_REQUIRED = "relogin_required", "Relogin Required"

    profile = models.ForeignKey(Profile, on_delete=models.CASCADE, related_name="session_events")
    warming_day = models.IntegerField(null=True, blank=True)
    date = models.DateField()
    session_status = models.CharField(max_length=20, choices=SessionStatus.choices)
    session_dropped_at = models.DateTimeField(null=True, blank=True)
    error_reason = models.TextField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Profile session event"
        verbose_name_plural = "Profile session events"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["profile", "date"]),
            models.Index(fields=["session_status"]),
            models.Index(fields=["warming_day"]),
            models.Index(fields=["date"]),
        ]

    def __str__(self):
        return f"{self.profile.profile_name} — Day {self.warming_day} — {self.get_session_status_display()} ({self.date})"
