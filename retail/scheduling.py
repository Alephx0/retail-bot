"""Device-local group schedules; persisted occurrence keys prevent repeat starts."""
from datetime import datetime, timedelta


def occurrences(schedule, current):
    configured = schedule.get("configured_at")
    if not configured:
        return []
    anchor = datetime.fromisoformat(configured).astimezone().replace(tzinfo=None)
    local = current.astimezone().replace(tzinfo=None)
    days = schedule.get("days", [])
    result = []
    for index, slot in enumerate(schedule.get("slots", [])):
        hour, minute = map(int, slot["start"].split(":"))
        if days:
            dates = [local.date() - timedelta(days=offset) for offset in (1, 0)]
        else:
            candidate = anchor.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= anchor:
                candidate += timedelta(days=1)
            dates = [candidate.date()]
        for day in dates:
            start = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
            if days and start.weekday() not in days:
                continue
            stop_hour, stop_minute = map(int, slot["stop"].split(":"))
            stop = start.replace(hour=stop_hour, minute=stop_minute)
            if stop <= start:
                stop += timedelta(days=1)
            if start >= anchor and start <= local < stop:
                result.append((f"{configured}/{index}/{day.isoformat()}", stop.isoformat()))
    return result
