"""APScheduler registration adapter."""


class APSchedulerRegistrar:
    def __init__(self, scheduler):
        self.scheduler = scheduler

    def register_daily_after_close(self, func, timezone: str = "US/Eastern", hour: int = 16, minute: int = 30):
        return self.scheduler.add_job(
            func,
            "cron",
            day_of_week="mon-fri",
            hour=hour,
            minute=minute,
            timezone=timezone,
        )

    def register_monthly_rd_agent_discovery(
        self,
        func,
        timezone: str = "US/Eastern",
        day: int = 1,
        hour: int = 18,
        minute: int = 0,
    ):
        return self.scheduler.add_job(
            func,
            "cron",
            day=day,
            hour=hour,
            minute=minute,
            timezone=timezone,
        )
