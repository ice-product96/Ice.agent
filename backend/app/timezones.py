"""IANA timezone normalization for cron jobs and employee profiles."""

from __future__ import annotations

import logging
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# Common non-IANA labels users enter in UI.
TIMEZONE_ALIASES: dict[str, str] = {
    "utc": "UTC",
    "gmt": "UTC",
    "msk": "Europe/Moscow",
    "moscow": "Europe/Moscow",
    "europe/moscow": "Europe/Moscow",
    "yekaterinburg": "Asia/Yekaterinburg",
    "ekaterinburg": "Asia/Yekaterinburg",
    "utc+5": "Asia/Yekaterinburg",
    "utc+05:00": "Asia/Yekaterinburg",
    "gmt+5": "Asia/Yekaterinburg",
    "gmt+05:00": "Asia/Yekaterinburg",
    "+5": "Asia/Yekaterinburg",
    "utc+3": "Europe/Moscow",
    "utc+03:00": "Europe/Moscow",
    "gmt+3": "Europe/Moscow",
    "utc+4": "Asia/Yekaterinburg",  # legacy Samara; close enough for cron
    "utc+7": "Asia/Novosibirsk",
    "utc+8": "Asia/Shanghai",
    "utc+9": "Asia/Tokyo",
}


def normalize_timezone(name: str | None, *, default: str = "UTC") -> str:
    """Return a valid IANA timezone key; map friendly aliases; fall back to default."""
    raw = str(name or "").strip()
    if not raw:
        return default
    try:
        ZoneInfo(raw)
        return raw
    except (ZoneInfoNotFoundError, ValueError):
        pass
    alias = TIMEZONE_ALIASES.get(raw.lower().replace(" ", ""))
    if alias:
        logger.info("Normalized timezone %r -> %s", raw, alias)
        return alias
    match = re.fullmatch(r"(?i)(?:utc|gmt)?([+-])(\d{1,2})(?::(\d{2}))?", raw.replace(" ", ""))
    if match:
        sign, hours, minutes = match.group(1), int(match.group(2)), int(match.group(3) or 0)
        offset_minutes = hours * 60 + minutes
        if sign == "-":
            offset_minutes = -offset_minutes
        mapped = _offset_to_iana(offset_minutes)
        if mapped:
            logger.info("Normalized offset timezone %r -> %s", raw, mapped)
            return mapped
    logger.warning("Unknown timezone %r; falling back to %s", raw, default)
    return default


def _offset_to_iana(offset_minutes: int) -> str | None:
    # Fixed-offset shortcuts for Russia / common user inputs.
    table = {
        180: "Europe/Moscow",       # UTC+3
        240: "Asia/Yekaterinburg",  # UTC+4 (no DST; Samara is +4)
        300: "Asia/Yekaterinburg",  # UTC+5
        360: "Asia/Omsk",           # UTC+6
        420: "Asia/Novosibirsk",    # UTC+7
        480: "Asia/Irkutsk",        # UTC+8
        540: "Asia/Yakutsk",        # UTC+9
        600: "Asia/Vladivostok",    # UTC+10
        660: "Asia/Magadan",        # UTC+11
        720: "Asia/Kamchatka",      # UTC+12
    }
    return table.get(offset_minutes)


def zoneinfo(name: str | None, *, default: str = "UTC") -> ZoneInfo:
    return ZoneInfo(normalize_timezone(name, default=default))
