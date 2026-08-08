"""Small, migration-free helpers for time-limited audience subscriptions.

The existing schema already has ``User.plan`` and a JSON ``User.settings``
column. The expiry metadata lives in that JSON object so enabling the simulated
checkout does not require a schema migration or alter unrelated tables.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional
import uuid

from django.db import transaction
from django.utils import timezone
from django.conf import settings
from django.utils.dateparse import parse_datetime

from .models import User, PaymentTransaction, PlayConfiguration

PREMIUM_EXPIRY_KEY = "premium_expires_at"
PREMIUM_ACTIVATED_KEY = "premium_activated_at"
PREMIUM_GATEWAY_KEY = "premium_gateway"
PREMIUM_DURATION = timedelta(days=30)


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
    """Return the canonical server expiry for a timed Premium subscription.

    Older checkout code could stack calendar months when the activation endpoint
    was called repeatedly. Checkout-created subscriptions are capped to exactly
    30 days from their latest recorded activation so already-affected accounts
    are corrected on their next authenticated request.
    """

    settings_data = user.settings if isinstance(user.settings, dict) else {}
    expiry = _coerce_aware(settings_data.get(PREMIUM_EXPIRY_KEY))
    activated_at = _coerce_aware(settings_data.get(PREMIUM_ACTIVATED_KEY))
    gateway = str(settings_data.get(PREMIUM_GATEWAY_KEY) or "").strip()
    if expiry is not None and activated_at is not None and gateway:
        return min(expiry, activated_at + PREMIUM_DURATION)
    return expiry


def normalize_expired_premium(user: User, *, now: Optional[datetime] = None) -> bool:
    """Normalize timed Premium metadata and return whether Premium is active.

    Legacy Premium accounts without expiry metadata remain Premium. Timed
    checkout subscriptions are corrected to a maximum of 30 days from the most
    recent activation, then downgraded when that canonical expiry has passed.
    """

    if user.plan != User.PLAN_PREMIUM:
        return False

    settings_data = dict(user.settings or {})
    raw_expiry = _coerce_aware(settings_data.get(PREMIUM_EXPIRY_KEY))
    expiry = premium_expires_at(user)
    if expiry is None:
        return True

    expiry_was_corrected = raw_expiry is not None and expiry != raw_expiry
    current = now or timezone.now()
    if expiry > current:
        if expiry_was_corrected:
            settings_data[PREMIUM_EXPIRY_KEY] = expiry.isoformat()
            user.settings = settings_data
            user.save(update_fields=["settings"])
        return True

    settings_data.pop(PREMIUM_EXPIRY_KEY, None)
    settings_data.pop(PREMIUM_GATEWAY_KEY, None)
    user.plan = User.PLAN_FREE
    user.settings = settings_data
    user.save(update_fields=["plan", "settings"])
    return False


def activate_one_month_premium(user: User, *, gateway: str = "zarinpal") -> datetime:
    """Reset Premium to exactly 30 days from this successful payment."""

    now = timezone.now()
    expiry = now + PREMIUM_DURATION
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


def activate_one_month_premium_locked(
    user_id: int, *, gateway: str = "zarinpal"
) -> tuple[User, datetime, PaymentTransaction]:
    """Record a successful mock checkout and activate Premium atomically.

    The transaction row intentionally mirrors the row a future verified
    Zarinpal callback should create. Replacing the mock payment step later can
    therefore preserve the admin finance/reporting contract.
    """

    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        config = PlayConfiguration.objects.order_by('-updated_at', '-pk').first()
        fallback_price = Decimal(str(getattr(settings, 'PREMIUM_PLAN_PRICE', 0) or 0))
        amount = config.premium_plan_price if config is not None else fallback_price
        payment = PaymentTransaction.objects.create(
            user=user,
            transaction_id=f"mock-{gateway}-{uuid.uuid4().hex}",
            amount=amount,
            status=PaymentTransaction.STATUS_SUCCESS,
            payment_method=gateway,
            description='خرید اشتراک پریمیوم ۳۰ روزه',
            description_en='30-day Premium subscription purchase',
        )
        expiry = activate_one_month_premium(user, gateway=gateway)
    return user, expiry, payment
