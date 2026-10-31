"""Deterministic jurisdiction-aware contract notice deadline calculations."""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SUPPORTED_JURISDICTIONS = {"US-FEDERAL", "GB-ENG-WLS", "IN-NATIONAL"}


def jurisdiction_holidays(jurisdiction: str | None, year: int) -> set[date]:
    """Return supported national/public holidays; local exceptions belong in custom holidays."""
    key = (jurisdiction or "").strip().upper()
    if not key:
        return set()
    if key not in SUPPORTED_JURISDICTIONS:
        raise ValueError(f"Unsupported notice jurisdiction '{jurisdiction}'. Supported: {', '.join(sorted(SUPPORTED_JURISDICTIONS))}")
    if key == "US-FEDERAL":
        holidays = {
            _observed(date(year, 1, 1)), _nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3),
            _last_weekday(year, 5, 0), _observed(date(year, 6, 19)), _observed(date(year, 7, 4)),
            _nth_weekday(year, 9, 0, 1), _nth_weekday(year, 10, 0, 2), _observed(date(year, 11, 11)),
            _nth_weekday(year, 11, 3, 4), _observed(date(year, 12, 25)),
        }
        if year < 2021:
            holidays.discard(_observed(date(year, 6, 19)))
        return holidays
    if key == "GB-ENG-WLS":
        easter = _easter_sunday(year)
        holidays = {
            _observed(date(year, 1, 1)), easter - timedelta(days=2), easter + timedelta(days=1),
            _nth_weekday(year, 5, 0, 1), _last_weekday(year, 5, 0), _last_weekday(year, 8, 0),
        }
        christmas, boxing = date(year, 12, 25), date(year, 12, 26)
        holidays.update(_substitute_days(christmas, boxing))
        return holidays
    return {date(year, 1, 26), date(year, 8, 15), date(year, 10, 2)}


def calculate_deadline(
    expiration: date,
    notice_days: int,
    day_type: str,
    *,
    timezone: str = "UTC",
    jurisdiction: str | None = None,
    holidays: set[date] | None = None,
    weekend_days: set[int] | None = None,
) -> dict:
    if notice_days < 0:
        raise ValueError("notice_days must be non-negative")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {timezone}") from exc
    weekends = {5, 6} if weekend_days is None else set(weekend_days)
    if any(day < 0 or day > 6 for day in weekends):
        raise ValueError("weekend weekdays must be from 0 (Monday) to 6 (Sunday)")
    custom = set(holidays or set())
    jurisdiction_dates: set[date] = set()
    if jurisdiction:
        for year in range(expiration.year - 12, expiration.year + 1):
            jurisdiction_dates.update(jurisdiction_holidays(jurisdiction, year))
    excluded = custom | jurisdiction_dates
    if day_type == "calendar":
        due = expiration - timedelta(days=notice_days)
        counted = [due + timedelta(days=offset) for offset in range(notice_days)]
        used_holidays: set[date] = set()
    elif day_type == "business":
        due = expiration
        counted = []
        used_holidays = set()
        while len(counted) < notice_days:
            due -= timedelta(days=1)
            if due.weekday() in weekends:
                continue
            if due in excluded:
                used_holidays.add(due)
                continue
            else:
                counted.append(due)
    else:
        raise ValueError("notice_day_type must be 'calendar' or 'business'")
    details = {
        "due_date": due.isoformat(),
        "expiration_date": expiration.isoformat(),
        "notice_days": notice_days,
        "day_type": day_type,
        "timezone": timezone,
        "jurisdiction": jurisdiction.upper() if jurisdiction else None,
        "weekend_weekdays": sorted(weekends),
        "holiday_source": "jurisdiction plus contract-specified dates" if jurisdiction else "contract-specified dates",
        "holiday_dates_used": sorted(d.isoformat() for d in used_holidays),
        "calculation": (
            f"Subtract {notice_days} calendar days from {expiration.isoformat()}"
            if day_type == "calendar" else
            f"Count {notice_days} qualifying business days backward from {expiration.isoformat()}, "
            f"skipping weekdays {sorted(weekends)} and listed holidays"
        ),
    }
    return details


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _substitute_days(*days: date) -> set[date]:
    result: set[date] = set()
    occupied = set(days)
    for day in sorted(days):
        candidate = day
        while candidate.weekday() >= 5 or candidate in occupied or candidate in result:
            candidate += timedelta(days=1)
        result.add(candidate)
    return result


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + (occurrence - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)
