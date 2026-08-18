from __future__ import annotations

from typing import Iterable

from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.permissions import BasePermission

from .models import User


EMPLOYEE_ROLES = {User.ROLE_MANAGER, User.ROLE_SUPERVISOR}
SESSION_VERSION_KEY = '_employee_session_version'

# ``*.view`` is the master switch for a screen. Dashboard and Employees are
# intentionally not represented here because they are owner-admin only.
SCREEN_PERMISSIONS = {
    'users': ('view', 'edit', 'ban'),
    'artists': ('view', 'edit', 'kyc', 'verify', 'ban', 'delete'),
    'release_add': ('view', 'edit', 'publish'),
    'releases': ('view', 'review', 'publish', 'takedown', 'delete'),
    'songs': ('view', 'edit', 'takedown', 'delete'),
    'albums': ('view', 'edit', 'takedown', 'delete'),
    'tags': ('view', 'edit', 'delete'),
    'plans': ('view', 'price', 'payout', 'ads'),
    'finance': ('view', 'payments', 'earnings', 'payouts', 'payout_review', 'payout_pay'),
    'content': ('view', 'promotions', 'banners', 'audio_ads'),
    'playlists': ('view', 'playlists', 'sections'),
    'support': ('view', 'tickets', 'reports'),
}
ALLOWED_PERMISSION_KEYS = frozenset(
    f'{screen}.{action}'
    for screen, actions in SCREEN_PERMISSIONS.items()
    for action in actions
)


def is_platform_admin(user) -> bool:
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if not getattr(user, 'is_active', False) or getattr(user, 'is_banned', False):
        return False
    if getattr(user, 'is_superuser', False):
        return True
    roles = set(getattr(user, 'roles', None) or [])
    return bool(getattr(user, 'is_staff', False) and User.ROLE_ADMIN in roles)


def employee_role(user) -> str | None:
    """Employee access exists only for a clean, sole manager/supervisor role."""
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return None
    roles = set(getattr(user, 'roles', None) or [])
    if roles == {User.ROLE_MANAGER}:
        return User.ROLE_MANAGER
    if roles == {User.ROLE_SUPERVISOR}:
        return User.ROLE_SUPERVISOR
    return None


def is_employee(user) -> bool:
    return employee_role(user) is not None


def is_employee_account(user) -> bool:
    """Privacy check also hides malformed legacy employee-like accounts."""
    if not user:
        return False
    roles = set(getattr(user, 'roles', None) or [])
    return bool(roles & EMPLOYEE_ROLES) and not getattr(user, 'is_staff', False)


def normalize_employee_permissions(value) -> dict[str, bool]:
    raw = value if isinstance(value, dict) else {}
    normalized = {key: bool(raw.get(key, False)) for key in ALLOWED_PERMISSION_KEYS}

    # A few screens have no useful read-only/empty state. Keep their shape
    # coherent even for legacy or manually-crafted permission payloads.
    if normalized.get('release_add.view'):
        normalized['release_add.edit'] = True
    if normalized.get('artists.verify'):
        normalized['artists.kyc'] = True
    if normalized.get('release_add.publish'):
        normalized['release_add.edit'] = True
    if normalized.get('finance.payout_review') or normalized.get('finance.payout_pay'):
        normalized['finance.payouts'] = True

    if normalized.get('finance.view') and not any(normalized.get(key) for key in (
        'finance.payments', 'finance.earnings', 'finance.payouts'
    )):
        normalized['finance.view'] = False
    if normalized.get('content.view') and not any(normalized.get(key) for key in (
        'content.promotions', 'content.banners', 'content.audio_ads'
    )):
        normalized['content.view'] = False
    if normalized.get('support.view') and not any(normalized.get(key) for key in (
        'support.tickets', 'support.reports'
    )):
        normalized['support.view'] = False

    # Master screen switch is authoritative; hidden stale action bits cannot live
    # behind a disabled screen. Run this last so structural normalization above
    # cannot leave orphaned action permissions behind.
    for screen, actions in SCREEN_PERMISSIONS.items():
        view_key = f'{screen}.view'
        if not normalized[view_key]:
            for action in actions:
                normalized[f'{screen}.{action}'] = False
    return normalized


def employee_session_version(user) -> int:
    raw = getattr(user, 'permissions', None)
    if not isinstance(raw, dict):
        return 1
    try:
        return max(1, int(raw.get(SESSION_VERSION_KEY, 1) or 1))
    except (TypeError, ValueError):
        return 1


def employee_permissions_payload(value, *, session_version: int | None = None) -> dict:
    payload = normalize_employee_permissions(value)
    if session_version is None:
        raw = value if isinstance(value, dict) else {}
        try:
            session_version = int(raw.get(SESSION_VERSION_KEY, 1) or 1)
        except (TypeError, ValueError):
            session_version = 1
    payload[SESSION_VERSION_KEY] = max(1, int(session_version))
    return payload


def bump_employee_session_version(user, *, save: bool = True) -> int:
    next_version = employee_session_version(user) + 1
    user.permissions = employee_permissions_payload(
        getattr(user, 'permissions', None), session_version=next_version
    )
    if save:
        user.save(update_fields=['permissions'])
    return next_version


def has_employee_permission(user, key: str) -> bool:
    if key not in ALLOWED_PERMISSION_KEYS or not is_employee(user):
        return False
    return normalize_employee_permissions(getattr(user, 'permissions', None)).get(key, False)


def require_employee_permission(user, key: str) -> None:
    if is_platform_admin(user):
        return
    if not has_employee_permission(user, key):
        raise PermissionDenied('شما اجازه انجام این عملیات را ندارید.')


def _any(user, keys: Iterable[str]) -> bool:
    return any(has_employee_permission(user, key) for key in keys)


def _groups_for_request(request, view) -> tuple[tuple[str, ...], ...] | None:
    """Return AND-of-OR permission groups. Unknown endpoints deny employees."""
    name = view.__class__.__name__
    method = request.method.upper()

    simple = {
        'AdminUserListView': {'GET': (('users.view',),)},
        'AdminUserDetailView': {
            'GET': (('users.view',),), 'PUT': (('users.edit',),), 'PATCH': (('users.edit',),),
            # hard user deletion stays owner-only
        },
        'AdminUserBanView': {'POST': (('users.ban', 'artists.ban'),)},
        'AdminArtistListView': {'GET': (('artists.view', 'release_add.view'),), 'POST': (('artists.edit',),)},
        'AdminArtistDetailView': {'GET': (('artists.view',),), 'DELETE': (('artists.delete',),)},
        'AdminPendingArtistListView': {'GET': (('artists.view',),)},
        'AdminPendingArtistDetailView': {'GET': (('artists.view',),), 'PATCH': (('artists.verify',),)},
        'AdminSongListView': {'GET': (('songs.view', 'release_add.view', 'content.promotions', 'playlists.playlists', 'playlists.sections'),)},
        'AdminAlbumListView': {'GET': (('albums.view', 'playlists.sections'),)},
        'AdminAlbumDetailView': {'GET': (('albums.view',),), 'PATCH': (('albums.edit',),), 'PUT': (('albums.edit',),)},
        'AdminTaxonomyListView': {'GET': (('tags.view', 'release_add.view'),), 'POST': (('tags.edit',),)},
        'AdminTaxonomyDetailView': {'GET': (('tags.view',),), 'PATCH': (('tags.edit',),), 'DELETE': (('tags.delete',),)},
        'AdminFinanceSummaryView': {'GET': (('finance.view',),)},
        'AdminArtistEarningsListView': {'GET': (('finance.earnings',),)},
        'AdminPaymentTransactionListView': {'GET': (('finance.payments',),)},
        'AdminDepositRequestListView': {'GET': (('finance.payouts',),)},
        'AdminSearchSectionListView': {'GET': (('playlists.view',),), 'POST': (('playlists.sections',),)},
        'AdminSearchSectionDetailView': {'GET': (('playlists.view',),), 'PATCH': (('playlists.sections',),), 'DELETE': (('playlists.sections',),)},
        'AdminEventPlaylistListView': {'GET': (('playlists.view',),), 'POST': (('playlists.sections',),)},
        'AdminEventPlaylistDetailView': {'GET': (('playlists.view',),), 'PATCH': (('playlists.sections',),), 'DELETE': (('playlists.sections',),)},
        'AdminPlaylistBuilderView': {'GET': (('playlists.playlists',),), 'POST': (('playlists.playlists',),)},
        'AdminPlaylistListView': {'GET': (('playlists.view',),), 'POST': (('playlists.playlists',),)},
        'AdminPlaylistDetailView': {'GET': (('playlists.view',),), 'PATCH': (('playlists.playlists',),), 'DELETE': (('playlists.playlists',),)},
        'AdminSupportTicketListView': {'GET': (('support.tickets',),)},
        'AdminSupportTicketDetailView': {'GET': (('support.tickets',),), 'PATCH': (('support.tickets',),)},
        'AdminReportListView': {'GET': (('support.reports',),)},
        'AdminReportDetailView': {'GET': (('support.reports',),), 'PUT': (('support.reports',),)},
        'AdminSongPromotionListView': {'GET': (('content.promotions',),), 'POST': (('content.promotions',),)},
        'AdminSongPromotionDetailView': {'GET': (('content.promotions',),), 'PATCH': (('content.promotions',),), 'DELETE': (('content.promotions',),)},
        'AdminBannerAdListView': {'GET': (('content.banners',),), 'POST': (('content.banners',),)},
        'AdminBannerAdDetailView': {'GET': (('content.banners',),), 'PATCH': (('content.banners',),), 'DELETE': (('content.banners',),)},
        'AdminAudioAdListView': {'GET': (('content.audio_ads',),), 'POST': (('content.audio_ads',),)},
        'AdminAudioAdDetailView': {'GET': (('content.audio_ads',),), 'PATCH': (('content.audio_ads',),), 'DELETE': (('content.audio_ads',),)},
        'AdminReleaseListView': {'GET': (('release_add.view', 'releases.view'),), 'POST': (('release_add.edit',),)},
        'AdminReleaseDetailView': {'GET': (('release_add.view', 'releases.view'),), 'PATCH': (('release_add.edit',),), 'DELETE': (('releases.delete',),)},
        'AdminReleaseTracksView': {'POST': (('release_add.edit',),), 'DELETE': (('release_add.edit',),)},
        'AdminReleaseTrackUploadView': {'POST': (('release_add.edit',),)},
        'AdminReleaseArtworkView': {'POST': (('release_add.edit',),)},
        'AdminReleaseValidateView': {'POST': (('release_add.edit', 'release_add.publish', 'releases.review', 'releases.publish'),)},
    }
    if name in simple and method in simple[name]:
        return simple[name][method]

    if name == 'AdminUserSearchView' and method == 'GET':
        typ = str(request.query_params.get('type') or 'audience').strip()
        if typ == 'audience':
            return (('users.view',),)
        if typ == 'artist':
            return (('artists.view',),)
        if typ == 'pend_artist':
            return (('artists.view',),)
        return None

    if name == 'AdminArtistDetailView' and method in {'PATCH', 'PUT'}:
        if method == 'PATCH':
            keys = set(getattr(request, 'data', {}) or {})
            if keys and keys <= {'verified'}:
                return (('artists.verify',),)
        return (('artists.edit',),)

    if name == 'AdminPlayConfigurationView':
        if method == 'GET':
            return (('plans.view',),)
        if method == 'POST':
            groups = []
            keys = set(getattr(request, 'data', {}) or {})
            if 'premium_plan_price' in keys:
                groups.append(('plans.price',))
            if keys & {'per_normal_play_pay', 'per_premium_play_pay', 'minimum_payout_amount'}:
                groups.append(('plans.payout',))
            if 'ad_frequency' in keys:
                groups.append(('plans.ads',))
            return tuple(groups) if groups else None

    if name == 'AdminDepositRequestDetailView':
        if method == 'GET':
            return (('finance.payouts',),)
        if method == 'PATCH':
            data = getattr(request, 'data', {}) or {}
            groups = []
            if 'status' in data:
                new_status = str(data.get('status') or '').strip()
                groups.append(('finance.payout_pay',) if new_status == 'done' else ('finance.payout_review',))
            if 'transaction_id' in data:
                groups.append(('finance.payout_pay',))
            return tuple(groups) if groups else None

    if name == 'AdminSongDetailView':
        if method == 'GET':
            return (('songs.view', 'release_add.view'),)
        if method in {'PATCH', 'PUT'}:
            return (('songs.edit', 'release_add.edit'),)
        if method == 'DELETE':
            mode = str(request.query_params.get('mode') or 'hard').strip().lower()
            return (('songs.takedown',),) if mode == 'soft' else (('songs.delete',),)

    if name == 'AdminAlbumDetailView' and method == 'DELETE':
        mode = str(request.query_params.get('mode') or 'hard').strip().lower()
        return (('albums.takedown',),) if mode == 'soft' else (('albums.delete',),)

    if name == 'AdminReleaseActionView' and method == 'POST':
        action = str(getattr(request, 'data', {}).get('action') or '').strip()
        if action in {'request_changes', 'reject', 'approve', 'return_to_review'}:
            return (('releases.review',),)
        if action in {'schedule', 'publish'}:
            return (('release_add.publish', 'releases.publish'),)
        if action == 'take_down':
            return (('releases.takedown',),)
        if action == 'reopen':
            return (('releases.review', 'releases.takedown'),)
        return None

    return None


def _employee_token_is_current(request, user) -> bool:
    token = getattr(request, 'auth', None)
    try:
        claim = int(token.get('admin_session_version', 0))
    except (AttributeError, TypeError, ValueError):
        return False
    return claim == employee_session_version(user)


class IsAdminPanelSession(BasePermission):
    message = 'این حساب دسترسی به پنل مدیریت ندارد.'

    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        if is_platform_admin(user):
            return True
        if not user or not getattr(user, 'is_authenticated', False):
            return False
        if is_employee(user) and (not getattr(user, 'is_active', False) or getattr(user, 'is_banned', False)):
            raise AuthenticationFailed('نشست کارمند غیرفعال شده است. دوباره وارد شوید.')
        if not getattr(user, 'is_active', False) or getattr(user, 'is_banned', False):
            return False
        if not is_employee(user):
            return False
        if not _employee_token_is_current(request, user):
            raise AuthenticationFailed('نشست کارمند منقضی شده است. دوباره وارد شوید.')
        return True


class IsOwnerAdmin(BasePermission):
    message = 'این بخش فقط برای مدیر اصلی در دسترس است.'

    def has_permission(self, request, view):
        return is_platform_admin(getattr(request, 'user', None))


class IsAdminPanelUser(BasePermission):
    message = 'شما اجازه انجام این عملیات را ندارید.'

    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        if is_platform_admin(user):
            return True
        if not user or not getattr(user, 'is_authenticated', False):
            return False
        if is_employee(user) and (not getattr(user, 'is_active', False) or getattr(user, 'is_banned', False)):
            raise AuthenticationFailed('نشست کارمند غیرفعال شده است. دوباره وارد شوید.')
        if not getattr(user, 'is_active', False) or getattr(user, 'is_banned', False):
            return False
        if not is_employee(user):
            return False
        if not _employee_token_is_current(request, user):
            raise AuthenticationFailed('نشست کارمند منقضی شده است. دوباره وارد شوید.')
        groups = _groups_for_request(request, view)
        if not groups:
            return False
        return all(_any(user, group) for group in groups)


def panel_identity(user) -> dict:
    role = employee_role(user)
    return {
        'id': user.pk,
        'phone_number': user.phone_number,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'roles': list(user.roles or []),
        'is_staff': bool(user.is_staff),
        'is_owner_admin': is_platform_admin(user),
        'is_employee': bool(role),
        'employee_role': role,
        'permissions': normalize_employee_permissions(user.permissions) if role else {},
    }
