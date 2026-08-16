from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.employee import CONSULT_CMD_RE, heartbeat_cron, period_bounds


def test_heartbeat_cron_divisors() -> None:
    assert heartbeat_cron(15) == "*/15 * * * *"
    assert heartbeat_cron(10) == "*/10 * * * *"
    assert heartbeat_cron(7).startswith("*/")


def test_period_bounds_day() -> None:
    now = datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)
    start, end = period_bounds("day", now, ZoneInfo("UTC"))
    assert start.day == 15
    assert (end - start).days == 1


def test_consult_command_parse() -> None:
    match = CONSULT_CMD_RE.match("/approve 12")
    assert match is not None
    assert match.group(1).lower() == "approve"
    assert match.group(2) == "12"
    match = CONSULT_CMD_RE.match("/answer 3 yes, do it")
    assert match is not None
    assert match.group(3).strip() == "yes, do it"
