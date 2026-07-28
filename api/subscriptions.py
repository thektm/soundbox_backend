"""Small, migration-free helpers for time-limited audience subscriptions.

The existing schema already has ``User.plan`` and a JSON ``User.settings``
column. The expiry metadata lives in that JSON object so enabling the simulated
checkout does not require a schema migration or alter unrelated tables.
"""

from __future__ import annotations

import calendar
from datetime import datetime
from typing import Optional

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import User

PREMIUM_EXPIRY_KEY = "premium_expires_at"
PREMIUM_ACTIVATED_KEY = "premium_activated_at"
PREMIUM_GATEWAY_KEY = "premium_gateway"


def _coerce_aware(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = parse_datetime(value.strip())
    else:
        return None
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def premium_expires_at(user: User) -> Optional[datetime]:
    settings_data = user.settings if isinstance(user.settings, dict) else {}
    return _coerce_aware(settings_data.get(PREMIUM_EXPIRY_KEY))


def _add_calendar_month(value: datetime) -> datetime:
    """Return the same wall-clock time one calendar month later.

    End-of-month dates are clamped to the final valid day in the destination
    month (for example, January 31 becomes February 28/29).
    """

    next_month_index = value.month + 1
    year = value.year + (next_month_index - 1) // 12
    month = (next_month_index - 1) % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def normalize_expired_premium(user: User, *, now: Optional[datetime] = None) -> bool:
    """Downgrade an expired timed subscription and return active status.

    Legacy premium accounts without expiry metadata remain premium; only plans
    created by the timed checkout flow are automatically expired.
    """

    if user.plan != User.PLAN_PREMIUM:
        return False

    expiry = premium_expires_at(user)
    if expiry is None:
        return True

    current = now or timezone.now()
    if expiry > current:
        return True

    settings_data = dict(user.settings or {})
    settings_data.pop(PREMIUM_EXPIRY_KEY, None)
    settings_data.pop(PREMIUM_GATEWAY_KEY, None)
    user.plan = User.PLAN_FREE
    user.settings = settings_data
    user.save(update_fields=["plan", "settings"])
    return False


def activate_one_month_premium(user: User, *, gateway: str = "zarinpal") -> datetime:
    """Activate Premium until exactly one calendar month from this request."""

    now = timezone.now()
    current_expiry = premium_expires_at(user)
    base = current_expiry if current_expiry and current_expiry > now else now
    expiry = _add_calendar_month(base)
    settings_data = dict(user.settings or {})
    settings_data.update(
        {
            PREMIUM_EXPIRY_KEY: expiry.isoformat(),
            PREMIUM_ACTIVATED_KEY: now.isoformat(),
            PREMIUM_GATEWAY_KEY: gateway,
        }
    )
    user.plan = User.PLAN_PREMIUM
    user.settings = settings_data
    user.save(update_fields=["plan", "settings"])
    return expiry


def activate_one_month_premium_locked(user_id: int, *, gateway: str = "zarinpal") -> tuple[User, datetime]:
    """Serialize repeated checkout completions for the same account."""

    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        expiry = activate_one_month_premium(user, gateway=gateway)
    return user, expiry
