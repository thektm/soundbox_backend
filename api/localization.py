"""Request-aware bilingual helpers for Farsi/English API responses.

Farsi remains the canonical/default value for existing database columns. Each
translatable model field has a sibling ``*_en`` column. Public serializers keep
returning the existing field name, localized for the request, and also expose
``*_fa``/``*_en`` so clients can switch without guessing field semantics.
"""

from __future__ import annotations

from typing import Any

SUPPORTED_LANGUAGES = {"fa", "en"}
DEFAULT_LANGUAGE = "fa"


def get_request_language(request: Any = None) -> str:
    if request is None:
        return DEFAULT_LANGUAGE

    query_language = str(getattr(request, "query_params", {}).get("lang") or "").lower()
    query_language = query_language.split("-", 1)[0]
    if query_language in SUPPORTED_LANGUAGES:
        return query_language

    header_language = (
        request.headers.get("X-App-Language")
        or request.headers.get("Accept-Language")
        or ""
    ).lower()
    if header_language.startswith("en"):
        return "en"
    if header_language.startswith("fa") or header_language.startswith("fa-ir"):
        return "fa"

    user = getattr(request, "user", None)
    settings = getattr(user, "settings", None)
    if isinstance(settings, dict) and settings.get("language") in SUPPORTED_LANGUAGES:
        return settings["language"]
    return DEFAULT_LANGUAGE


def localized_value(obj: Any, field: str, request: Any = None, language: str | None = None) -> Any:
    """Return a model field in the requested language with safe fallbacks."""
    if obj is None:
        return None
    lang = language or get_request_language(request)
    fa_value = getattr(obj, field, None)
    en_value = getattr(obj, f"{field}_en", None)
    if lang == "en":
        return en_value if en_value not in (None, "", [], {}) else fa_value
    return fa_value if fa_value not in (None, "", [], {}) else en_value


GENERATED_TERM_FA_TO_EN = {
    "پاپ": "Pop",
    "راک": "Rock",
    "سنتی": "Traditional",
    "رپ": "Rap",
    "الکترونیک": "Electronic",
    "جز": "Jazz",
    "بلوز": "Blues",
    "متال": "Metal",
    "کلاسیک": "Classical",
    "فولک": "Folk",
    "شاد": "Happy",
    "غمگین": "Sad",
    "عاشقانه": "Romantic",
    "انرژیک": "Energetic",
    "آرام": "Calm",
    "هیجان‌انگیز": "Exciting",
    "مذهبی": "Spiritual",
    "پارتی": "Party",
    "تمرکز": "Focus",
    "خواب": "Sleep",
    "ورزشی": "Workout",
    "موتورسواری": "Driving",
    "نوستالژیک": "Nostalgic",
    "الهام‌بخش": "Inspirational",
    "رقص": "Dance",
}


GENERATED_FA_TO_EN = {
    "یک کاربر": "A user",
    "داغِ همین حالا": "Trending Right Now",
    "پرشنونده‌ترین انتخاب‌های ۲۴ ساعت گذشته": "Most-played picks from the last 24 hours",
    "تازه رسیده‌ها": "Fresh Arrivals",
    "ریلیزهای تازه با چیدمانی که مرتب نو می‌شود": "Fresh releases in a regularly refreshed mix",
    "محبوب‌های صداباکس": "SedaBox Favorites",
    "ترک‌های امتحان‌پس‌داده برای یک پخش بی‌وقفه": "Proven favorites for uninterrupted listening",
    "کشف‌های تازه": "Fresh Discoveries",
    "کمتر تکراری، تازه‌تر و مناسب پیدا کردن صدای بعدی": "Less repetition, more freshness, and a new sound to discover",
    "یک جریان کوتاه و منسجم برای حال‌وهوای الآن": "A short, cohesive flow for your current mood",
}


def translate_generated_text(text: str) -> str:
    """Translate server-owned generated labels; never alter user-authored text."""
    if not text:
        return text
    if text in GENERATED_FA_TO_EN:
        return GENERATED_FA_TO_EN[text]
    if text in GENERATED_TERM_FA_TO_EN:
        return GENERATED_TERM_FA_TO_EN[text]
    if text.startswith("موج "):
        term = text[4:]
        return f"{GENERATED_TERM_FA_TO_EN.get(term, term)} Wave"
    if text.startswith("یک میکس تازه از فضای "):
        term = text.removeprefix("یک میکس تازه از فضای ")
        return f"A fresh mix inspired by {GENERATED_TERM_FA_TO_EN.get(term, term)}"
    if text.endswith(" برای این لحظه"):
        term = text.removesuffix(" برای این لحظه")
        return f"{GENERATED_TERM_FA_TO_EN.get(term, term)} for This Moment"

    # Historical notification rows were created before ``text_en`` existed.
    # Translate only the server-owned sentence template; user/artist names and
    # content titles are preserved exactly as authored.
    import re

    notification_patterns = (
        (r"^(.+?) شروع به دنبال کردن شما کرد\.?$", lambda m: f"{m.group(1)} started following you."),
        (r"^(.+?) لیست پخش ['«](.+?)['»] شما را لایک کرد\.?$", lambda m: f"{m.group(1)} liked your playlist '{m.group(2)}'."),
        (r"^آهنگ جدید ['«](.+?)['»] از (.+?) منتشر شد!?$", lambda m: f"New song '{m.group(1)}' by {m.group(2)} is out!"),
        (r"^آلبوم جدید ['«](.+?)['»] از (.+?) منتشر شد!?$", lambda m: f"New album '{m.group(1)}' by {m.group(2)} is out!"),
    )
    for pattern, formatter in notification_patterns:
        match = re.match(pattern, text)
        if match:
            return formatter(match)
    return text

# API-owned response messages. Content fields such as song/playlist titles are
# deliberately excluded from this map so user-authored text is never rewritten.
API_MESSAGE_EN_TO_FA = {
    "A new short stream link has been generated": "یک لینک کوتاه جدید برای پخش ایجاد شد",
    "A user with that phone number already exists": "کاربری با این شماره تلفن از قبل وجود دارد",
    "Account temporarily locked": "حساب کاربری موقتاً قفل شده است",
    "Ad already submitted": "این تبلیغ قبلاً ثبت شده است",
    "Advertisement must be watched before accessing this stream": "برای دسترسی به این پخش، ابتدا باید تبلیغ را مشاهده کنید",
    "Album and its songs deleted successfully": "آلبوم و آهنگ‌های آن با موفقیت حذف شدند",
    "Album created successfully": "آلبوم با موفقیت ایجاد شد",
    "Album not found": "آلبوم پیدا نشد",
    "Album updated successfully": "آلبوم با موفقیت به‌روزرسانی شد",
    "All notifications marked as read": "همه اعلان‌ها به‌عنوان خوانده‌شده علامت‌گذاری شدند",
    "Artist not found": "هنرمند پیدا نشد",
    "Artist profile not found": "پروفایل هنرمند پیدا نشد",
    "Artist profile not found or user is not an artist": "پروفایل هنرمند پیدا نشد یا کاربر هنرمند نیست",
    "Both 'current_password' and 'new_password' are required.": "وارد کردن رمز عبور فعلی و رمز عبور جدید الزامی است.",
    "Current password is incorrect": "رمز عبور فعلی نادرست است",
    "Current password is incorrect.": "رمز عبور فعلی نادرست است.",
    "Either user_id or artist_id must be provided.": "باید شناسه کاربر یا شناسه هنرمند وارد شود.",
    "Failed to delete song record": "حذف اطلاعات آهنگ ناموفق بود",
    "Failed to send OTP SMS": "ارسال پیامک رمز یک‌بارمصرف ناموفق بود",
    "High quality streaming is only available for premium users.": "پخش با کیفیت بالا فقط برای کاربران پریمیوم در دسترس است.",
    "Invalid action. Use 'remove' or 'delete'": "عملیات نامعتبر است. از remove یا delete استفاده کنید",
    "Invalid credentials": "اطلاعات ورود نادرست است",
    "Invalid or unauthorized one-time token": "توکن یک‌بارمصرف نامعتبر است یا مجوز دسترسی ندارد",
    "Invalid or unauthorized stream URL": "نشانی پخش نامعتبر است یا مجوز دسترسی ندارد",
    "Invalid or unauthorized stream token": "توکن پخش نامعتبر است یا مجوز دسترسی ندارد",
    "Invalid quality choice.": "کیفیت انتخاب‌شده نامعتبر است.",
    "Invalid refresh token": "توکن تمدید نامعتبر است",
    "Invalid submit_id": "شناسه ثبت نامعتبر است",
    "Invalid type parameter. Use audience|artist|pend_artist": "پارامتر نوع نامعتبر است. از audience، artist یا pend_artist استفاده کنید",
    "Invalid type.": "نوع نامعتبر است.",
    "Invalid type. Must be song, artist, album, playlist, or user.": "نوع نامعتبر است. باید آهنگ، هنرمند، آلبوم، پلی‌لیست یا کاربر باشد.",
    "Invalid unique_otplay_id": "شناسه یکتای پخش نامعتبر است",
    "Max file size is 5MB": "حداکثر حجم فایل ۵ مگابایت است",
    "New password must be at least 6 characters long.": "رمز عبور جدید باید حداقل ۶ نویسه باشد.",
    "No artist profile found": "پروفایل هنرمندی پیدا نشد",
    "No plays found to request deposit": "هیچ پخشی برای ثبت درخواست واریز پیدا نشد",
    "No rules found.": "قوانینی پیدا نشد.",
    "No valid OTP found": "رمز یک‌بارمصرف معتبری پیدا نشد",
    "Not an employee.": "کاربر عضو کارکنان نیست.",
    "Not authorized": "اجازه دسترسی ندارید",
    "Not found": "پیدا نشد",
    "Not found.": "پیدا نشد.",
    "Notification marked as read": "اعلان به‌عنوان خوانده‌شده علامت‌گذاری شد",
    "OK": "موفق",
    "OTP attempts exceeded": "تعداد تلاش‌های رمز یک‌بارمصرف بیش از حد مجاز است",
    "OTP sent": "رمز یک‌بارمصرف ارسال شد",
    "One of song, artist or reported_user must be provided.": "یکی از آهنگ، هنرمند یا کاربر گزارش‌شده باید مشخص شود.",
    "Only artists can access this endpoint": "فقط هنرمندان به این بخش دسترسی دارند",
    "Only one of user_id or artist_id should be provided.": "فقط یکی از شناسه کاربر یا شناسه هنرمند باید وارد شود.",
    "Password changed successfully": "رمز عبور با موفقیت تغییر کرد",
    "Phone already registered": "این شماره تلفن قبلاً ثبت شده است",
    "Phone not registered": "این شماره تلفن ثبت نشده است",
    "Play count recorded successfully": "تعداد پخش با موفقیت ثبت شد",
    "Playlist not found": "پلی‌لیست پیدا نشد",
    "Playlist not found.": "پلی‌لیست پیدا نشد.",
    "Please listen to this brief advertisement": "لطفاً این تبلیغ کوتاه را گوش کنید",
    "Please wait before requesting another OTP": "پیش از درخواست رمز یک‌بارمصرف جدید کمی صبر کنید",
    "Provide only one of song, artist or reported_user.": "فقط یکی از آهنگ، هنرمند یا کاربر گزارش‌شده را مشخص کنید.",
    "Rule not found.": "قانون پیدا نشد.",
    "SedaBox user not found": "کاربر صداباکس پیدا نشد",
    "Session has been revoked or expired": "نشست شما لغو شده یا منقضی شده است",
    "Social account not found.": "حساب شبکه اجتماعی پیدا نشد.",
    "Song already in playlist": "این آهنگ از قبل در پلی‌لیست وجود دارد",
    "Song deleted successfully": "آهنگ با موفقیت حذف شد",
    "Song not found": "آهنگ پیدا نشد",
    "Song removed from album": "آهنگ از آلبوم حذف شد",
    "Stream link expired or unauthorized for this user": "لینک پخش منقضی شده یا برای این کاربر مجاز نیست",
    "Submission already exists. Use PATCH to update.": "این ثبت از قبل وجود دارد. برای به‌روزرسانی از PATCH استفاده کنید.",
    "The provided OTP is invalid.": "رمز یک‌بارمصرف واردشده نامعتبر است.",
    "This account has been banned.": "این حساب کاربری مسدود شده است.",
    "This one-time access URL has already been used": "این نشانی دسترسی یک‌بارمصرف قبلاً استفاده شده است",
    "This one-time access URL has expired": "این نشانی دسترسی یک‌بارمصرف منقضی شده است",
    "This play ID has already been used": "این شناسه پخش قبلاً استفاده شده است",
    "This stream URL has already been used": "این نشانی پخش قبلاً استفاده شده است",
    "This stream token has already been used": "این توکن پخش قبلاً استفاده شده است",
    "User is not an artist": "کاربر هنرمند نیست",
    "User must have manager or supervisor role.": "کاربر باید نقش مدیر یا سرپرست داشته باشد.",
    "You already have a pending deposit request": "شما از قبل یک درخواست واریز در انتظار دارید",
    "You cannot follow yourself.": "نمی‌توانید خودتان را دنبال کنید.",
    "You must finish watching the previous advertisement": "ابتدا باید مشاهده تبلیغ قبلی را کامل کنید",
    "Your account has been banned.": "حساب کاربری شما مسدود شده است.",
    "audio_file is required": "فایل صوتی الزامی است",
    "followed": "دنبال شد",
    "liked": "پسندیده شد",
    "ok": "موفق",
    "otp_sent": "رمز یک‌بارمصرف ارسال شد",
    "password_changed": "رمز عبور تغییر کرد",
    "password_reset": "رمز عبور بازنشانی شد",
    "phone is required": "شماره تلفن الزامی است",
    "playlist not found": "پلی‌لیست پیدا نشد",
    "q parameter is required": "پارامتر جست‌وجو الزامی است",
    "refreshToken is required to keep the current session": "برای حفظ نشست فعلی، refreshToken الزامی است",
    "saved": "ذخیره شد",
    "song_id is required": "شناسه آهنگ الزامی است",
    "song_ids is required (list of integers)": "شناسه‌های آهنگ الزامی است (فهرستی از اعداد صحیح)",
    "submit_id is required": "شناسه ثبت الزامی است",
    "unfollowed": "دنبال‌کردن لغو شد",
    "unique_otplay_id, city, and country are required": "شناسه یکتای پخش، شهر و کشور الزامی هستند",
    "unliked": "پسند لغو شد",
    "unsaved": "از ذخیره خارج شد",
    "user_id is required": "شناسه کاربر الزامی است",
    # Common Django REST Framework validation messages.
    "This field is required.": "این فیلد الزامی است.",
    "This field is required when auth_type is existing_artist.": "وقتی نوع احراز هویت existing_artist است، وارد کردن این فیلد الزامی است.",
    "This field may not be blank.": "این فیلد نمی‌تواند خالی باشد.",
    "This field may not be null.": "این فیلد نمی‌تواند تهی باشد.",
    "Invalid input.": "ورودی نامعتبر است.",
    "Enter a valid email address.": "یک نشانی ایمیل معتبر وارد کنید.",
    "A valid integer is required.": "یک عدد صحیح معتبر وارد کنید.",
    "A valid number is required.": "یک عدد معتبر وارد کنید.",
    "National ID must be exactly 10 digits": "کد ملی باید دقیقاً ۱۰ رقم باشد",
    "Phone number must be in local format starting with 09 and 11 digits": "شماره تلفن باید در قالب محلی، با ۰۹ شروع شود و ۱۱ رقم داشته باشد",
    "Incorrect type. Expected URL string, received str.": "نوع داده نامعتبر است.",
    "Authentication credentials were not provided.": "اطلاعات احراز هویت ارائه نشده است.",
    "You do not have permission to perform this action.": "اجازه انجام این عملیات را ندارید.",
    "Method not allowed.": "این روش درخواست مجاز نیست.",
    "Not acceptable.": "پاسخ قابل‌قبولی برای درخواست پیدا نشد.",
    "Unsupported media type.": "نوع رسانه پشتیبانی نمی‌شود.",
    "Request was throttled.": "تعداد درخواست‌ها بیش از حد مجاز است.",
}

API_MESSAGE_FA_TO_EN = {value: key for key, value in API_MESSAGE_EN_TO_FA.items()}

_DYNAMIC_MESSAGE_PREFIXES_EN_TO_FA = {
    "Failed to save artist: ": "ذخیره هنرمند ناموفق بود: ",
    "Profile image upload failed: ": "بارگذاری تصویر پروفایل ناموفق بود: ",
    "Banner image upload failed: ": "بارگذاری تصویر بنر ناموفق بود: ",
    "User ": "کاربر ",
}

# Technical values must remain byte-for-byte stable for client-side branching.
_TECHNICAL_RESPONSE_KEYS = {
    "code", "status_code", "token", "access", "refresh", "refreshToken",
    "url", "stream_url", "submit_id", "unique_otplay_id", "id",
}
_MESSAGE_RESPONSE_KEYS = {"error", "errors", "message", "messages", "detail", "non_field_errors"}


def localize_api_message(value: str, language: str) -> str:
    """Localize an API-owned message while preserving interpolated details."""
    if not value:
        return value
    if language == "en":
        return API_MESSAGE_FA_TO_EN.get(value, value)

    exact = API_MESSAGE_EN_TO_FA.get(value)
    if exact is not None:
        return exact

    for prefix, translated_prefix in _DYNAMIC_MESSAGE_PREFIXES_EN_TO_FA.items():
        if value.startswith(prefix):
            # The user-ban message has a fixed English suffix around a variable ID.
            if prefix == "User " and value.endswith(" has been banned and their content deleted."):
                user_value = value[len(prefix):-len(" has been banned and their content deleted.")]
                return f"کاربر {user_value} مسدود شد و محتوای او حذف گردید."
            return translated_prefix + value[len(prefix):]

    # Parameterized DRF messages remain readable without maintaining every number.
    import re

    patterns = (
        (r"^Ensure this field has at least (\d+) characters\.$", lambda m: f"این فیلد باید حداقل {m.group(1)} نویسه داشته باشد."),
        (r"^Ensure this field has no more than (\d+) characters\.$", lambda m: f"این فیلد نباید بیشتر از {m.group(1)} نویسه داشته باشد."),
        (r"^Ensure this value is greater than or equal to (.+)\.$", lambda m: f"این مقدار باید بزرگ‌تر یا مساوی {m.group(1)} باشد."),
        (r"^Ensure this value is less than or equal to (.+)\.$", lambda m: f"این مقدار باید کوچک‌تر یا مساوی {m.group(1)} باشد."),
        (r'^Invalid pk "(.+)" - object does not exist\.$', lambda m: f"شناسه «{m.group(1)}» نامعتبر است؛ موردی با این شناسه وجود ندارد."),
        (r"^Expected a list of items but got type \"(.+)\"\.$", lambda m: f"انتظار می‌رفت فهرستی از موارد دریافت شود، اما نوع «{m.group(1)}» ارسال شد."),
        (r"^Only (.+) files are allowed$", lambda m: f"فقط فایل‌های {m.group(1)} مجاز هستند"),
        (r"^Invalid token\.$", lambda _m: "توکن نامعتبر است."),
        (r"^Token is invalid or expired$", lambda _m: "توکن نامعتبر یا منقضی شده است"),
    )
    for pattern, formatter in patterns:
        match = re.match(pattern, value)
        if match:
            return formatter(match)
    return value


def localize_api_payload(
    value: Any,
    language: str,
    *,
    status_code: int = 200,
    message_context: bool = False,
    key: str | None = None,
) -> Any:
    """Recursively localize response-owned messages, not domain content."""
    if isinstance(value, dict):
        localized = {}
        for child_key, child_value in value.items():
            child_key_string = str(child_key)
            technical = (
                child_key_string in _TECHNICAL_RESPONSE_KEYS
                or child_key_string.endswith("_id")
                or child_key_string.endswith("_url")
                or child_key_string.endswith("_token")
            )
            child_message_context = (
                not technical
                and (
                    message_context
                    or child_key_string in _MESSAGE_RESPONSE_KEYS
                    or status_code >= 400
                )
            )
            localized[child_key] = localize_api_payload(
                child_value,
                language,
                status_code=status_code,
                message_context=child_message_context,
                key=child_key_string,
            )
        return localized
    if isinstance(value, list):
        return [
            localize_api_payload(
                item,
                language,
                status_code=status_code,
                message_context=message_context,
                key=key,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            localize_api_payload(
                item,
                language,
                status_code=status_code,
                message_context=message_context,
                key=key,
            )
            for item in value
        )
    if isinstance(value, str) and message_context:
        return localize_api_message(value, language)
    return value


# Localizing in the renderer catches ordinary responses and DRF-generated
# validation/authentication errors through one consistent path.
try:
    from rest_framework.renderers import JSONRenderer

    class LocalizedJSONRenderer(JSONRenderer):
        def render(self, data, accepted_media_type=None, renderer_context=None):
            context = renderer_context or {}
            request = context.get("request")
            response = context.get("response")
            language = get_request_language(request)
            status_code = int(getattr(response, "status_code", 200) or 200)
            localized_data = localize_api_payload(
                data,
                language,
                status_code=status_code,
                message_context=status_code >= 400 or isinstance(data, str),
            )
            return super().render(localized_data, accepted_media_type, context)
except ImportError:  # Allows static tooling to inspect this module without DRF installed.
    LocalizedJSONRenderer = None  # type: ignore[assignment]
