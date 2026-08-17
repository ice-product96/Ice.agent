from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.timezones import normalize_timezone, zoneinfo


def test_normalize_utc_plus_five() -> None:
    assert normalize_timezone("UTC+5") == "Asia/Yekaterinburg"


def test_normalize_iana_passthrough() -> None:
    assert normalize_timezone("Europe/Moscow") == "Europe/Moscow"


def test_normalize_unknown_falls_back() -> None:
    assert normalize_timezone("Not/A_Real_Zone") == "UTC"


def test_cron_trigger_accepts_normalized_timezone() -> None:
    tz = normalize_timezone("UTC+5")
    CronTrigger.from_crontab("*/15 * * * *", timezone=ZoneInfo(tz))


def test_zoneinfo_helper() -> None:
    assert str(zoneinfo("UTC+5")) == "Asia/Yekaterinburg"
