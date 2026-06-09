import traceback
from datetime import datetime


class TaskLogger:
    def __init__(self, task=None):
        self.task = task

    def console(self, msg):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self.task:
            profile_id = getattr(getattr(self.task, "profile", None), "ml_profile_id", "-") or "-"
            tag = f"[T:{self.task.id}][P:{profile_id}]"
        else:
            tag = "[T:-][P:-]"
        print(f"[{timestamp}]{tag} {msg}")

    async def info(self, msg):
        self.console(msg)
        if self.task:
            try:
                from user.services import add_task_log
                await add_task_log(self.task, msg, level="INFO")
            except Exception as e:
                self.console(f"[WARNING] TaskLogger: failed to write to DB: {e}")

    async def warning(self, msg):
        self.console(msg)
        if self.task:
            try:
                from user.services import add_task_log
                await add_task_log(self.task, msg, level="WARNING")
            except Exception as e:
                self.console(f"[WARNING] TaskLogger: failed to write to DB: {e}")

    async def error(self, msg, exc=None):
        self.console(msg)
        if exc is not None:
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        if self.task:
            try:
                from user.services import add_task_log
                await add_task_log(self.task, msg, level="ERROR")
            except Exception as e:
                self.console(f"[WARNING] TaskLogger: failed to write to DB: {e}")