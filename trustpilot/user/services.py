from asgiref.sync import sync_to_async
from .models import UserTaskManager, UserTaskLog

@sync_to_async
def add_task_log(task: UserTaskManager, message: str, level: str = UserTaskLog.LogLevel.INFO):
    """Creates a log entry for a given UserTaskManager instance."""
    return UserTaskLog.objects.create(
        task=task,
        message=message,
        level=level,
    )