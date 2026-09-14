"""Telegram deep-link video bot.

The bot uses long polling, so it does not need a public web server or webhook.
Video sources can be Telegram file IDs, public video URLs, or local file paths.
"""

from __future__ import annotations

import json
import logging
import os
import re
import asyncio
from datetime import datetime, timedelta, timezone
from secrets import choice
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

LOGGER = logging.getLogger("telegram_video_bot")
PAYLOAD_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_VIDEO_MAP_PATH = Path("videos.json")
DEFAULT_USERS_PATH = Path("users.json")
DEFAULT_PAYMENTS_PATH = Path("payments.json")
DEFAULT_SETTINGS_PATH = Path("settings.json")
DEFAULT_BOT_USERNAME = "Minicarrbot"
DEFAULT_REQUIRED_CHANNEL = "@Team_Being_Richer"
DEFAULT_REQUIRED_CHANNEL_URL = "https://t.me/Team_Being_Richer"
CHECK_MEMBERSHIP_CALLBACK = "check_membership"
PREMIUM_CALLBACK = "premium"
PLAN_CALLBACK_PREFIX = "plan:"
PAYMENT_SUBMIT_CALLBACK = "payment_submit:"
PAYMENT_APPROVE_CALLBACK = "payment_approve:"
PAYMENT_REJECT_CALLBACK = "payment_reject:"
REFER_CALLBACK = "refer"
AD_CONTINUE_CALLBACK = "ad_continue"
REFERRAL_PREFIX = "ref_"
DEFAULT_FREE_DAILY_LIMIT = 1
DEFAULT_PREMIUM_PRICE = "Configure premium price"
DEFAULT_PREMIUM_DURATION_DAYS = 30
DEFAULT_REFERRAL_REWARD_DAYS = 7
DEFAULT_AD_MESSAGE = "Please view this short promotion before receiving the video."
DEFAULT_PLAN_PRICES = {
    "7d": "Configure price",
    "30d": "Configure price",
    "lifetime": "Configure price",
}
DEFAULT_PLAN_DEFINITIONS = {
    "7d": {"label": "7 days", "days": 7},
    "30d": {"label": "30 days", "days": 30},
    "lifetime": {"label": "Lifetime", "days": None},
}
DAILY_LIMIT_MESSAGE = (
    "⛔ Daily limit reached.\n"
    "You can watch 1 video per day.\n"
    "Try again after the limit resets."
)
VIDEO_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
VIDEO_CODE_LENGTH = 6
PLACEHOLDER_SOURCES = {
    "",
    "PASTE_TELEGRAM_FILE_ID_OR_PUBLIC_URL_HERE",
}


class VideoConfigError(ValueError):
    """Raised when the configured video map cannot be used."""


def load_video_map(path: Path) -> dict[str, dict[str, str]]:
    """Load and validate the payload-to-video mapping."""
    if not path.exists():
        raise VideoConfigError(
            f"Video map not found at {path}. Create it from videos.example.json."
        )

    try:
        raw_map: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise VideoConfigError(f"Invalid JSON in {path}: {error}") from error

    if not isinstance(raw_map, dict):
        raise VideoConfigError("The video map must be a JSON object keyed by payload.")

    video_map: dict[str, dict[str, str]] = {}
    for payload, raw_entry in raw_map.items():
        if not isinstance(payload, str) or not PAYLOAD_PATTERN.fullmatch(payload):
            raise VideoConfigError(
                f"Invalid payload {payload!r}; use 1-64 letters, numbers, hyphens, or underscores."
            )

        if isinstance(raw_entry, str):
            video_source = raw_entry
            caption = ""
        elif isinstance(raw_entry, dict):
            video_source = raw_entry.get("video", "")
            caption = raw_entry.get("caption", "")
        else:
            raise VideoConfigError(
                f"Entry for {payload!r} must be a video string or an object with a video field."
            )

        if not isinstance(video_source, str):
            raise VideoConfigError(f"Video source for {payload!r} must be a string.")
        if not isinstance(caption, str):
            raise VideoConfigError(f"Caption for {payload!r} must be a string.")

        if video_source in PLACEHOLDER_SOURCES:
            LOGGER.warning(
                "Skipping %s because its video source is still a placeholder",
                payload,
            )
            continue

        video_map[payload] = {"video": video_source, "caption": caption}

    return video_map


def load_users(path: Path) -> dict[str, dict[str, Any]]:
    """Load persistent user, premium, usage, and referral state."""
    if not path.exists():
        return {}

    try:
        raw_users: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise VideoConfigError(f"Invalid JSON in {path}: {error}") from error

    if not isinstance(raw_users, dict):
        raise VideoConfigError("The users file must be a JSON object keyed by user ID.")

    return {
        str(user_id): record
        for user_id, record in raw_users.items()
        if isinstance(record, dict)
    }


def default_user_record() -> dict[str, Any]:
    """Return the persisted shape for a newly seen Telegram user."""
    return {
        "premium_until": None,
        "premium_lifetime": False,
        "usage_date": "",
        "usage_count": 0,
        "usage_started_at": None,
        "usage_reset_at": None,
        "referred_by": None,
        "referral_completed": False,
        "referral_count": 0,
    }


def get_user_record(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> dict[str, Any]:
    """Get or initialize one user's persistent record."""
    users: dict[str, dict[str, Any]] = context.application.bot_data["users"]
    user_key = str(user_id)
    if user_key not in users:
        users[user_key] = default_user_record()
    return users[user_key]


def save_json_atomic(path: Path, data: Any) -> None:
    """Write JSON through a same-directory replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def generate_video_code(existing_codes: set[str]) -> str:
    """Generate a short uppercase code that is not already in the map."""
    while True:
        code = "".join(choice(VIDEO_CODE_ALPHABET) for _ in range(VIDEO_CODE_LENGTH))
        if code not in existing_codes:
            return code


def save_video_map(path: Path, video_map: dict[str, dict[str, str]]) -> None:
    """Persist the complete video map with an atomic file replacement."""
    save_json_atomic(path, video_map)


def get_video_map(context: ContextTypes.DEFAULT_TYPE) -> dict[str, dict[str, str]]:
    """Read the map attached to the application at startup."""
    return context.application.bot_data["video_map"]


def get_users(context: ContextTypes.DEFAULT_TYPE) -> dict[str, dict[str, Any]]:
    """Read persistent user state attached to the application."""
    return context.application.bot_data["users"]


def get_user_lock(context: ContextTypes.DEFAULT_TYPE) -> asyncio.Lock:
    """Read the lock protecting user-state updates."""
    return context.application.bot_data["users_lock"]


def get_user_path(context: ContextTypes.DEFAULT_TYPE) -> Path:
    """Read the configured persistent user-state path."""
    return context.application.bot_data["users_path"]


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """Read non-secret monetization settings."""
    return context.application.bot_data["settings"]


def get_payments(context: ContextTypes.DEFAULT_TYPE) -> dict[str, dict[str, Any]]:
    """Read persistent manual-payment records."""
    return context.application.bot_data["payments"]


def get_payments_path(context: ContextTypes.DEFAULT_TYPE) -> Path:
    """Read the configured payment-record path."""
    return context.application.bot_data["payments_path"]


def get_settings_path(context: ContextTypes.DEFAULT_TYPE) -> Path:
    """Read the configured settings path."""
    return context.application.bot_data["settings_path"]


def get_payment_lock(context: ContextTypes.DEFAULT_TYPE) -> asyncio.Lock:
    """Read the lock protecting payment-record updates."""
    return context.application.bot_data["payments_lock"]


def get_pending_payment_plans(
    context: ContextTypes.DEFAULT_TYPE,
) -> dict[int, str]:
    """Read the selected Premium plan for each payment chat."""
    return context.application.bot_data["pending_payment_plans"]


def get_pending_payment_uploads(
    context: ContextTypes.DEFAULT_TYPE,
) -> dict[tuple[int, int], str]:
    """Read users who were asked to send a payment screenshot."""
    return context.application.bot_data["pending_payment_uploads"]


def get_admin_ids(context: ContextTypes.DEFAULT_TYPE) -> set[int]:
    """Read admin IDs loaded from the protected environment variable."""
    return context.application.bot_data["admin_ids"]


def load_payments(path: Path) -> dict[str, dict[str, Any]]:
    """Load pending and completed manual-payment records."""
    if not path.exists():
        return {}
    try:
        raw_payments: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise VideoConfigError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(raw_payments, dict):
        raise VideoConfigError("The payments file must be a JSON object.")
    return {
        str(payment_id): record
        for payment_id, record in raw_payments.items()
        if isinstance(record, dict)
    }


def load_settings(path: Path, defaults: dict[str, Any]) -> dict[str, Any]:
    """Load persisted admin settings over environment defaults."""
    settings = dict(defaults)
    if not path.exists():
        return settings
    try:
        raw_settings: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise VideoConfigError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(raw_settings, dict):
        raise VideoConfigError("The settings file must be a JSON object.")
    for key in settings:
        if key in raw_settings and isinstance(raw_settings[key], type(settings[key])):
            settings[key] = raw_settings[key]
    return settings


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""
    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO timestamp saved in user state."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_premium_user(record: dict[str, Any]) -> bool:
    """Return whether a user's premium entitlement is still active."""
    if record.get("premium_lifetime") is True:
        return True
    premium_until = parse_datetime(record.get("premium_until"))
    return premium_until is not None and premium_until > utc_now()


def get_plan(settings: dict[str, Any], plan_key: str) -> dict[str, Any] | None:
    """Return one validated configured plan."""
    if plan_key not in DEFAULT_PLAN_DEFINITIONS:
        return None
    configured = settings.get("plans", {}).get(plan_key)
    if not isinstance(configured, dict):
        return None
    definition = DEFAULT_PLAN_DEFINITIONS[plan_key]
    return {
        "key": plan_key,
        "label": definition["label"],
        "days": definition["days"],
        "price": str(configured.get("price", "Configure price")),
    }


def premium_keyboard() -> InlineKeyboardMarkup:
    """Build the premium and referral actions."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💳 Buy Premium",
                    callback_data=PREMIUM_CALLBACK,
                )
            ],
            [InlineKeyboardButton("🎁 Refer & Earn", callback_data=REFER_CALLBACK)],
        ]
    )


def payment_keyboard(plan_key: str, settings: dict[str, Any]) -> InlineKeyboardMarkup:
    """Build plan selectors and the screenshot submission action."""
    plan_buttons = [
        InlineKeyboardButton(
            f"{definition['label']} — {get_plan(settings, key)['price']}",
            callback_data=f"{PLAN_CALLBACK_PREFIX}{key}",
        )
        for key, definition in DEFAULT_PLAN_DEFINITIONS.items()
        if get_plan(settings, key) is not None
    ]
    return InlineKeyboardMarkup(
        [
            plan_buttons,
            [
                InlineKeyboardButton(
                    "📤 Submit Payment Screenshot",
                    callback_data=f"{PAYMENT_SUBMIT_CALLBACK}{plan_key}",
                )
            ],
        ]
    )


def ad_keyboard(ad_link: str) -> InlineKeyboardMarkup:
    """Build the configured ad link and honest continuation action."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 View Ad", url=ad_link)],
            [
                InlineKeyboardButton(
                    "✅ Continue",
                    callback_data=AD_CONTINUE_CALLBACK,
                )
            ],
        ]
    )


def save_user_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persist all user state without exposing credentials."""
    save_json_atomic(get_user_path(context), get_users(context))


def save_settings(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persist runtime-configurable settings."""
    save_json_atomic(get_settings_path(context), get_settings(context))


def integer_setting(name: str, default: int) -> int:
    """Read a positive non-secret integer configuration value."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer.") from error
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero.")
    return value


FREE_USAGE_WINDOW = timedelta(hours=24)


def refresh_free_usage_window(
    record: dict[str, Any], now: datetime
) -> bool:
    """Migrate and lazily reset a user's rolling 24-hour free-view window."""
    changed = False
    usage_count = int(record.get("usage_count", 0) or 0)
    started_at = parse_datetime(record.get("usage_started_at"))

    if started_at is None and usage_count > 0:
        legacy_date = record.get("usage_date")
        try:
            started_at = (
                datetime.fromisoformat(legacy_date).replace(tzinfo=timezone.utc)
                if isinstance(legacy_date, str) and legacy_date
                else now
            )
        except ValueError:
            started_at = now
        record["usage_started_at"] = started_at.isoformat()
        record["usage_reset_at"] = (started_at + FREE_USAGE_WINDOW).isoformat()
        changed = True

    if started_at is not None and now >= started_at + FREE_USAGE_WINDOW:
        record["usage_date"] = now.date().isoformat()
        record["usage_count"] = 0
        record["usage_started_at"] = None
        record["usage_reset_at"] = None
        changed = True

    return changed


def free_usage_available(record: dict[str, Any], now: datetime) -> bool:
    """Return whether the user has not consumed the current 24-hour allowance."""
    started_at = parse_datetime(record.get("usage_started_at"))
    if started_at is None:
        return int(record.get("usage_count", 0) or 0) < 1
    return now < started_at + FREE_USAGE_WINDOW


async def reserve_video_access(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> tuple[bool, bool]:
    """Reserve one free daily view, or allow unlimited premium access.

    Returns (allowed, is_premium). The reservation is persisted before the
    video request so concurrent updates cannot exceed the daily limit.
    """
    async with get_user_lock(context):
        record = get_user_record(context, user_id)
        if is_premium_user(record):
            save_user_state(context)
            return True, True

        now = utc_now()
        refresh_free_usage_window(record, now)
        if not free_usage_available(record, now):
            save_user_state(context)
            return False, False

        record["usage_date"] = now.date().isoformat()
        record["usage_count"] = 1
        record["usage_started_at"] = now.isoformat()
        record["usage_reset_at"] = (now + FREE_USAGE_WINDOW).isoformat()
        save_user_state(context)
        return True, False


async def release_video_access(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    """Return a reserved free view when Telegram could not send the video."""
    async with get_user_lock(context):
        record = get_user_record(context, user_id)
        now = utc_now()
        refresh_free_usage_window(record, now)
        if int(record.get("usage_count", 0)) > 0:
            record["usage_count"] = 0
            record["usage_started_at"] = None
            record["usage_reset_at"] = None
            save_user_state(context)


async def complete_referral(
    user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    """Grant a one-time Premium reward after a referred user joins."""
    async with get_user_lock(context):
        user_record = get_user_record(context, user_id)
        referrer_key = str(user_record.get("referred_by") or "")
        if (
            not referrer_key
            or user_record.get("referral_completed")
            or referrer_key == str(user_id)
        ):
            return None

        users = get_users(context)
        referrer = users.setdefault(referrer_key, default_user_record())
        now = utc_now()
        current_expiration = parse_datetime(referrer.get("premium_until"))
        reward_start = max(now, current_expiration or now)
        reward_days = get_settings(context)["referral_reward_days"]
        referrer["premium_until"] = (
            reward_start + timedelta(days=reward_days)
        ).isoformat()
        referrer["referral_count"] = int(referrer.get("referral_count", 0)) + 1
        user_record["referral_completed"] = True
        save_user_state(context)
        return int(referrer_key)


async def register_referral(
    user_id: int, referrer_id: int, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Remember the first valid referrer for a new user."""
    if user_id == referrer_id:
        return False

    async with get_user_lock(context):
        user_record = get_user_record(context, user_id)
        if user_record.get("referred_by") or user_record.get("referral_completed"):
            return False
        user_record["referred_by"] = str(referrer_id)
        save_user_state(context)
        return True


async def send_referral_details(
    message: Any, user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Send the user's personal referral link and reward explanation."""
    bot_username = os.getenv("TELEGRAM_BOT_USERNAME", DEFAULT_BOT_USERNAME).lstrip(
        "@"
    )
    reward_days = get_settings(context)["referral_reward_days"]
    required_channel = get_settings(context)["required_channel"]
    await message.reply_text(
        "🎁 Refer & Earn\n\n"
        f"Share this link with friends:\nhttps://t.me/{bot_username}?start={REFERRAL_PREFIX}{user_id}\n\n"
        f"You earn {reward_days} Premium days for each friend who joins "
        f"{required_channel} through your link."
    )


def get_pending_subscriptions(
    context: ContextTypes.DEFAULT_TYPE,
) -> dict[tuple[int, int], tuple[int, str, str]]:
    """Get payloads waiting for a membership check.

    The key is the chat and prompt-message ID. Keeping the payload server-side
    avoids putting arbitrary deep-link payloads into Telegram callback data.
    """
    return context.application.bot_data["pending_subscriptions"]


def subscription_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
) -> InlineKeyboardMarkup:
    """Build the required join and membership-check buttons."""
    channel_url = get_settings(context)["required_channel_url"]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 Join Channel",
                    url=channel_url,
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ Check",
                    callback_data=CHECK_MEMBERSHIP_CALLBACK,
                )
            ],
        ]
    )


async def is_required_channel_member(
    user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Return whether Telegram reports the user as an active channel member."""
    try:
        member = await context.bot.get_chat_member(
            chat_id=get_settings(context)["required_channel"],
            user_id=user_id,
        )
    except TelegramError:
        LOGGER.exception(
            "Could not verify membership for user %s in %s",
            user_id,
            get_settings(context)["required_channel"],
        )
        return False

    return member.status in {"member", "administrator", "creator"} or bool(
        getattr(member, "is_member", False)
    )


async def send_subscription_prompt(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    payload: str,
    user_id: int,
    action: str = "video",
) -> None:
    """Ask a user to join the channel before they can receive a video."""
    required_channel = get_settings(context)["required_channel"]
    prompt = await message.reply_text(
        f"Please join {required_channel} to receive this video, then tap Check.",
        reply_markup=subscription_keyboard(context),
    )
    get_pending_subscriptions(context)[
        (prompt.chat_id, prompt.message_id)
    ] = (user_id, payload, action)


async def send_configured_video(
    message: Any, video_entry: dict[str, str]
) -> bool:
    """Send a configured video from a file ID, URL, or local path."""
    video_source = video_entry["video"]
    caption = video_entry["caption"] or None
    local_path = Path(video_source)

    try:
        if local_path.is_file():
            with local_path.open("rb") as video_file:
                await message.reply_video(
                    video=InputFile(video_file, filename=local_path.name),
                    caption=caption,
                    supports_streaming=True,
                )
        else:
            await message.reply_video(
                video=video_source,
                caption=caption,
                supports_streaming=True,
            )
        return True
    except Exception:
        LOGGER.exception("Failed to send configured video")
        await message.reply_text(
            "I found that video, but Telegram could not send it right now."
        )
        return False


async def deliver_video_with_access(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    video_entry: dict[str, str],
) -> None:
    """Enforce Free/Premium access before sending a video."""
    allowed, is_premium = await reserve_video_access(context, user_id)
    if not allowed:
        await message.reply_text(
            DAILY_LIMIT_MESSAGE,
            reply_markup=premium_keyboard(),
        )
        return

    sent = await send_configured_video(message, video_entry)
    if not sent and not is_premium:
        await release_video_access(context, user_id)


async def available_video_access(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> tuple[bool, bool]:
    """Check whether a user may start a video request without consuming it."""
    async with get_user_lock(context):
        record = get_user_record(context, user_id)
        if is_premium_user(record):
            return True, True

        now = utc_now()
        changed = refresh_free_usage_window(record, now)
        if changed:
            save_user_state(context)
        return free_usage_available(record, now), False


async def send_ad_prompt(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    payload: str,
    user_id: int,
) -> None:
    """Show the configured ad link before a free video."""
    settings = get_settings(context)
    prompt = await message.reply_text(
        f"{settings['ad_message']}\n\n"
        "Open the configured ad, complete its action, then tap Continue. "
        "The bot does not fake or hide ad-click tracking.",
        reply_markup=ad_keyboard(str(settings["ad_link"])),
    )
    get_pending_subscriptions(context)[
        (prompt.chat_id, prompt.message_id)
    ] = (user_id, payload, "ad")


async def prepare_video_delivery(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    payload: str,
    video_entry: dict[str, str],
) -> None:
    """Apply the limit and optional ad gate before delivering a video."""
    allowed, is_premium = await available_video_access(context, user_id)
    if not allowed:
        await message.reply_text(
            DAILY_LIMIT_MESSAGE,
            reply_markup=premium_keyboard(),
        )
        return

    if not is_premium and str(get_settings(context)["ad_link"]).strip():
        await send_ad_prompt(message, context, payload, user_id)
        return

    await deliver_video_with_access(message, context, user_id, video_entry)


async def ad_continue_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Continue after the user confirms they completed the configured ad action."""
    query = update.callback_query
    if query is None or query.message is None:
        return

    pending_key = (query.message.chat_id, query.message.message_id)
    pending_request = get_pending_subscriptions(context).get(pending_key)
    if pending_request is None:
        await query.answer(
            "This request expired. Please open the video link again.",
            show_alert=True,
        )
        return

    expected_user_id, payload, action = pending_request
    if action != "ad" or query.from_user.id != expected_user_id:
        await query.answer(
            "This button belongs to the person who opened the video link.",
            show_alert=True,
        )
        return

    get_pending_subscriptions(context).pop(pending_key, None)
    await query.answer("Continuing to your video.")
    await query.message.edit_text("Ad step completed. Checking your access...")
    video_entry = get_video_map(context).get(payload)
    if video_entry is None:
        await query.message.reply_text("I couldn't find a video for that link.")
        return
    await deliver_video_with_access(
        query.message,
        context,
        expected_user_id,
        video_entry,
    )


async def send_main_menu(message: Any) -> None:
    """Show the bot's available user actions."""
    await message.reply_text(
        "Open a video deep link, or choose an option below.",
        reply_markup=premium_keyboard(),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route video and referral deep links."""
    message = update.effective_message
    if message is None:
        return

    payload = context.args[0] if context.args else ""
    if not payload:
        await send_main_menu(message)
        return

    user = update.effective_user
    if user is None:
        await message.reply_text("I couldn't identify your Telegram account.")
        return

    if payload.startswith(REFERRAL_PREFIX):
        referrer_value = payload.removeprefix(REFERRAL_PREFIX)
        if not referrer_value.isdigit():
            await message.reply_text("That referral link is not valid.")
            return

        referrer_id = int(referrer_value)
        await register_referral(user.id, referrer_id, context)
        if not await is_required_channel_member(user.id, context):
            await send_subscription_prompt(
                message,
                context,
                payload,
                user.id,
                action="referral",
            )
            return

        rewarded_referrer_id = await complete_referral(user.id, context)
        await message.reply_text(
            "Membership confirmed. Your referral has been recorded."
            if rewarded_referrer_id is not None
            else "Your referral has already been recorded.",
            reply_markup=premium_keyboard(),
        )
        if rewarded_referrer_id is not None:
            try:
                await context.bot.send_message(
                    chat_id=rewarded_referrer_id,
                    text=(
                        "🎉 Referral successful! You earned "
                        f"{get_settings(context)['referral_reward_days']} Premium days."
                    ),
                )
            except TelegramError:
                LOGGER.exception(
                    "Could not notify referrer %s", rewarded_referrer_id
                )
        return

    if not PAYLOAD_PATTERN.fullmatch(payload):
        await message.reply_text("That video link is not valid.")
        return

    video_entry = get_video_map(context).get(payload)
    if video_entry is None:
        await message.reply_text("I couldn't find a video for that link.")
        return

    if not await is_required_channel_member(user.id, context):
        await send_subscription_prompt(message, context, payload, user.id)
        return

    await prepare_video_delivery(
        message,
        context,
        user.id,
        payload,
        video_entry,
    )


async def check_membership(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Re-check channel membership and deliver the waiting video if allowed."""
    query = update.callback_query
    if query is None:
        return

    pending = get_pending_subscriptions(context)
    prompt_message = query.message
    if prompt_message is None:
        await query.answer("Please open the video link again.", show_alert=True)
        return

    pending_key = (prompt_message.chat_id, prompt_message.message_id)
    pending_request = pending.get(pending_key)
    if pending_request is None:
        await query.answer(
            "This request expired. Please open the video link again.",
            show_alert=True,
        )
        return

    expected_user_id, payload, action = pending_request
    if query.from_user.id != expected_user_id:
        await query.answer(
            "This button belongs to the person who opened the video link.",
            show_alert=True,
        )
        return

    if not await is_required_channel_member(query.from_user.id, context):
        await query.answer(
            "Please join the channel first, then tap Check again.",
            show_alert=True,
        )
        return

    pending.pop(pending_key, None)
    await query.answer("Membership confirmed.")

    if action == "referral":
        rewarded_referrer_id = await complete_referral(
            expected_user_id, context
        )
        await prompt_message.edit_text(
            "Membership confirmed. Your referral has been recorded."
            if rewarded_referrer_id is not None
            else "Your referral has already been recorded.",
            reply_markup=premium_keyboard(),
        )
        if rewarded_referrer_id is not None:
            try:
                await context.bot.send_message(
                    chat_id=rewarded_referrer_id,
                    text=(
                        "🎉 Referral successful! You earned "
                        f"{get_settings(context)['referral_reward_days']} Premium days."
                    ),
                )
            except TelegramError:
                LOGGER.exception(
                    "Could not notify referrer %s", rewarded_referrer_id
                )
        return

    await prompt_message.edit_text("Membership confirmed. Checking your access...")
    video_entry = get_video_map(context).get(payload)
    if video_entry is None:
        await prompt_message.reply_text("I couldn't find a video for that link.")
        return

    await prepare_video_delivery(
        prompt_message,
        context,
        expected_user_id,
        payload,
        video_entry,
    )


async def video_file_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save an uploaded video and reply with its deep link."""
    message = update.effective_message
    if message is None or message.video is None:
        return

    video_map = get_video_map(context)
    video_map_lock: asyncio.Lock = context.application.bot_data["video_map_lock"]
    video_map_path: Path = context.application.bot_data["video_map_path"]

    async with video_map_lock:
        code = generate_video_code(set(video_map))
        video_map[code] = {
            "video": message.video.file_id,
            "caption": message.caption or "",
        }
        try:
            save_video_map(video_map_path, video_map)
        except Exception:
            video_map.pop(code, None)
            LOGGER.exception("Failed to save uploaded video for code %s", code)
            await message.reply_text(
                "I couldn't save this video right now. Please try again."
            )
            return

    bot_username = os.getenv("TELEGRAM_BOT_USERNAME", DEFAULT_BOT_USERNAME).lstrip("@")
    link = f"https://t.me/{bot_username}?start={code}"
    await message.reply_text(
        "Video saved successfully\n"
        f"Code: {code}\n"
        f"Link: {link}"
    )


async def premium_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the configured Premium QR payment."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    await send_premium_offer(message, context)


async def refer_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show a user's personal Refer & Earn link."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    async with get_user_lock(context):
        get_user_record(context, user.id)
        save_user_state(context)
    await send_referral_details(message, user.id, context)


async def premium_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the configured Premium QR payment from an inline button."""
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    if query.message:
        await send_premium_offer(query.message, context)
    else:
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="Use /premium to view the payment QR code.",
        )


async def refer_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show Refer & Earn from the inline button."""
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    if query.message:
        await send_referral_details(query.message, query.from_user.id, context)


async def send_premium_offer(
    message: Any, context: ContextTypes.DEFAULT_TYPE, plan_key: str = "30d"
) -> None:
    """Send the configured QR image and selectable Premium plans."""
    settings = get_settings(context)
    plan = get_plan(settings, plan_key) or get_plan(settings, "30d")
    qr_source = str(settings["qr_image_path"])
    if plan is None:
        await message.reply_text(
            "Premium plans are not configured yet. Please contact the admin."
        )
        return
    plan_key = plan["key"]
    duration_text = (
        "Lifetime"
        if plan["days"] is None
        else f"{plan['days']} days"
    )
    upi_details = str(settings.get("upi_details", "")).strip()
    payment_details = (
        f"UPI ID: {upi_details}\n"
        if upi_details
        else "UPI details: Ask the admin to configure the UPI ID.\n"
    )
    ad_caption = (
        "💎 Premium\n\n"
        f"Selected plan: {plan['label']}\n"
        f"Price: {plan['price']}\n"
        f"Access: {duration_text}\n\n"
        f"{payment_details}"
        "Scan the QR, complete payment, then submit your payment screenshot."
    )

    if not qr_source:
        await message.reply_text(
            "Premium payment is not configured yet. Please contact the admin."
        )
        return

    try:
        qr_path = Path(qr_source)
        if qr_path.is_file():
            with qr_path.open("rb") as qr_file:
                await message.reply_photo(
                    photo=InputFile(qr_file, filename=qr_path.name),
                    caption=ad_caption,
                    reply_markup=payment_keyboard(plan_key, settings),
                )
        else:
            await message.reply_photo(
                photo=qr_source,
                caption=ad_caption,
                reply_markup=payment_keyboard(plan_key, settings),
            )
        get_pending_payment_plans(context)[message.chat_id] = plan_key
        except Exception as exc:
        LOGGER.exception("Could not send configured Premium QR image")
        await message.reply_text(
            f"QR error: {type(exc).__name__}: {exc}"
        )




def get_pending_payment_for_user(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> str | None:
    """Return an existing pending payment ID for a user, if any."""
    for payment_id, payment in get_payments(context).items():
        if (
            str(payment.get("user_id")) == str(user_id)
            and payment.get("status") == "pending"
        ):
            return payment_id
    return None


async def submit_payment(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Ask a user to send the screenshot for the selected plan."""
    query = update.callback_query
    if query is None:
        return

    plan_key = query.data.removeprefix(PAYMENT_SUBMIT_CALLBACK)
    plan = get_plan(get_settings(context), plan_key)
    if plan is None:
        await query.answer("This plan is no longer available.", show_alert=True)
        return

    await query.answer("Send your payment screenshot now.")
    if query.message:
        get_pending_payment_uploads(context)[
            (query.message.chat_id, query.from_user.id)
        ] = plan_key
        await query.message.reply_text(
            f"Please send the payment screenshot for the {plan['label']} plan "
            "in your next message."
        )


async def plan_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Refresh the QR screen for a newly selected Premium plan."""
    query = update.callback_query
    if query is None:
        return
    plan_key = query.data.removeprefix(PLAN_CALLBACK_PREFIX)
    if get_plan(get_settings(context), plan_key) is None:
        await query.answer("This plan is no longer available.", show_alert=True)
        return
    await query.answer("Plan selected.")
    if query.message:
        await send_premium_offer(query.message, context, plan_key)


def payment_admin_keyboard(payment_id: str) -> InlineKeyboardMarkup:
    """Build the admin review actions for one screenshot."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=f"{PAYMENT_APPROVE_CALLBACK}{payment_id}",
                ),
                InlineKeyboardButton(
                    "❌ Reject",
                    callback_data=f"{PAYMENT_REJECT_CALLBACK}{payment_id}",
                ),
            ]
        ]
    )


def payment_review_text(
    payment_id: str,
    user: Any,
    plan: dict[str, Any],
) -> str:
    """Build the admin-visible review text without exposing credentials."""
    username = f"@{user.username}" if user.username else "not set"
    return (
        "💳 Premium payment review\n"
        f"Payment: {payment_id}\n"
        f"User ID: {user.id}\n"
        f"Username: {username}\n"
        f"Selected plan: {plan['label']}\n"
        f"Price: {plan['price']}\n"
        "Screenshot attached below."
    )


async def payment_screenshot(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Save and forward a user's payment screenshot for admin review."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    chat_id = message.chat_id
    plan_key = get_pending_payment_uploads(context).get((chat_id, user.id))
    if plan_key is None:
        return
    plan = get_plan(get_settings(context), plan_key)
    if plan is None:
        get_pending_payment_uploads(context).pop((chat_id, user.id), None)
        await message.reply_text("That Premium plan is no longer available.")
        return

    screenshot_type: str
    screenshot_file_id: str
    if message.photo:
        screenshot_type = "photo"
        screenshot_file_id = message.photo[-1].file_id
    elif message.document:
        screenshot_type = "document"
        screenshot_file_id = message.document.file_id
    else:
        return

    async with get_payment_lock(context):
        payments = get_payments(context)
        payment_id = get_pending_payment_for_user(context, user.id)
        if payment_id is None:
            payment_id = f"PAY-{generate_video_code(set(payments))}"
            payments[payment_id] = {
                "user_id": user.id,
                "username": user.username or "",
                "status": "pending",
                "created_at": utc_now().isoformat(),
                "plan_key": plan_key,
                "plan_label": plan["label"],
                "price": plan["price"],
                "screenshot_type": screenshot_type,
                "screenshot_file_id": screenshot_file_id,
            }
        else:
            payment = payments[payment_id]
            payment.update(
                {
                    "username": user.username or "",
                    "created_at": utc_now().isoformat(),
                    "plan_key": plan_key,
                    "plan_label": plan["label"],
                    "price": plan["price"],
                    "screenshot_type": screenshot_type,
                    "screenshot_file_id": screenshot_file_id,
                }
            )
        save_json_atomic(get_payments_path(context), payments)
    get_pending_payment_uploads(context).pop((chat_id, user.id), None)

    await message.reply_text(
        f"Screenshot received for {plan['label']}. Payment {payment_id} "
        "is waiting for admin approval."
    )
    review_text = payment_review_text(payment_id, user, plan)
    review_markup = payment_admin_keyboard(payment_id)
    for admin_id in get_admin_ids(context):
        try:
            if screenshot_type == "photo":
                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=screenshot_file_id,
                    caption=review_text,
                    reply_markup=review_markup,
                )
            else:
                await context.bot.send_document(
                    chat_id=admin_id,
                    document=screenshot_file_id,
                    caption=review_text,
                    reply_markup=review_markup,
                )
        except TelegramError:
            LOGGER.exception("Could not forward payment screenshot to admin %s", admin_id)


async def image_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route images to payment review or admin QR replacement."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    caption = (message.caption or "").strip().lower()
    if user.id in get_admin_ids(context) and caption.startswith("/set_qr"):
        file_id = (
            message.photo[-1].file_id
            if message.photo
            else message.document.file_id
            if message.document
            else None
        )
        if file_id is not None:
            get_settings(context)["qr_image_path"] = file_id
            save_settings(context)
            await message.reply_text(
                "Premium QR code replaced with the uploaded image."
            )
            return

    await payment_screenshot(update, context)


async def activate_premium_for_user(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    duration_days: int | None = None,
    lifetime: bool = False,
) -> datetime:
    """Activate or extend Premium for a user."""
    async with get_user_lock(context):
        record = get_user_record(context, user_id)
        if lifetime:
            record["premium_lifetime"] = True
            record["premium_until"] = None
            save_user_state(context)
            return datetime.max.replace(tzinfo=timezone.utc)

        current_expiration = parse_datetime(record.get("premium_until"))
        start = max(utc_now(), current_expiration or utc_now())
        days = duration_days or int(get_settings(context)["premium_duration_days"])
        expiration = start + timedelta(days=days)
        record["premium_lifetime"] = False
        record["premium_until"] = expiration.isoformat()
        save_user_state(context)
        return expiration


async def approve_payment(
    payment_id: str, context: ContextTypes.DEFAULT_TYPE, duration_days: int | None = None
) -> int | None:
    """Approve one pending payment and activate its user's Premium."""
    async with get_payment_lock(context):
        payment = get_payments(context).get(payment_id)
        if payment is None or payment.get("status") != "pending":
            return None
        payment["status"] = "processing"
        save_json_atomic(get_payments_path(context), get_payments(context))

    user_id = int(payment["user_id"])
    plan = get_plan(get_settings(context), str(payment.get("plan_key", "")))
    if plan is None:
        async with get_payment_lock(context):
            payment["status"] = "pending"
            save_json_atomic(get_payments_path(context), get_payments(context))
        return None
    selected_days = duration_days if duration_days is not None else plan["days"]
    lifetime = selected_days is None
    expiration = await activate_premium_for_user(
        user_id,
        context,
        selected_days,
        lifetime=lifetime,
    )
    async with get_payment_lock(context):
        payment["status"] = "approved"
        payment["approved_at"] = utc_now().isoformat()
        payment["premium_until"] = (
            None if lifetime else expiration.isoformat()
        )
        save_json_atomic(get_payments_path(context), get_payments(context))
    try:
        expiry_text = "lifetime" if lifetime else expiration.date().isoformat()
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "✅ Payment approved. Premium is now active until "
                f"{expiry_text}."
            ),
        )
    except TelegramError:
        LOGGER.exception("Could not notify approved user %s", user_id)
    return user_id


async def reject_payment(
    payment_id: str, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    """Reject one pending payment without changing Premium state."""
    async with get_payment_lock(context):
        payment = get_payments(context).get(payment_id)
        if payment is None or payment.get("status") != "pending":
            return None
        payment["status"] = "rejected"
        payment["rejected_at"] = utc_now().isoformat()
        user_id = int(payment["user_id"])
        save_json_atomic(get_payments_path(context), get_payments(context))
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ Payment {payment_id} was rejected. Please contact the admin.",
        )
    except TelegramError:
        LOGGER.exception("Could not notify rejected user %s", user_id)
    return user_id


async def payment_approve_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Approve a payment from the admin notification button."""
    query = update.callback_query
    if query is None:
        return
    if query.from_user.id not in get_admin_ids(context):
        await query.answer("Admin access required.", show_alert=True)
        return

    payment_id = query.data.removeprefix(PAYMENT_APPROVE_CALLBACK)
    user_id = await approve_payment(payment_id, context)
    if user_id is None:
        await query.answer("Payment is missing or already processed.", show_alert=True)
        return

    await query.answer("Premium activated.")
    if query.message:
        await query.message.edit_caption(
            caption=f"✅ Payment {payment_id} approved for user {user_id}."
        )


async def payment_reject_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Reject a payment from the admin screenshot message."""
    query = update.callback_query
    if query is None:
        return
    if query.from_user.id not in get_admin_ids(context):
        await query.answer("Admin access required.", show_alert=True)
        return

    payment_id = query.data.removeprefix(PAYMENT_REJECT_CALLBACK)
    user_id = await reject_payment(payment_id, context)
    if user_id is None:
        await query.answer("Payment is missing or already processed.", show_alert=True)
        return

    await query.answer("Payment rejected.")
    if query.message:
        await query.message.edit_caption(
            caption=f"❌ Payment {payment_id} rejected for user {user_id}."
        )


async def payment_submit_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle the user's screenshot-submission button."""
    await submit_payment(update, context)


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check admin access without revealing the configured admin list."""
    user = update.effective_user
    if user is not None and user.id in get_admin_ids(context):
        return True
    message = update.effective_message
    if message:
        await message.reply_text("This command is available to admins only.")
    return False


def parse_target_user_id(arguments: list[str]) -> int | None:
    """Parse a Telegram user ID from an admin command."""
    if not arguments or not arguments[0].isdigit():
        return None
    user_id = int(arguments[0])
    return user_id if user_id > 0 else None


async def activate_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin command: /activate <user_id> [days]."""
    if not await is_admin(update, context):
        return
    message = update.effective_message
    if message is None:
        return
    user_id = parse_target_user_id(context.args)
    if user_id is None:
        await message.reply_text("Usage: /activate <user_id> [days]")
        return
    days = None
    if len(context.args) > 1:
        if not context.args[1].isdigit() or int(context.args[1]) <= 0:
            await message.reply_text("Days must be a positive integer.")
            return
        days = int(context.args[1])
    expiration = await activate_premium_for_user(user_id, context, days)
    await message.reply_text(
        f"Premium activated for {user_id} until {expiration.date().isoformat()}."
    )


async def remove_premium_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin command: /remove_premium <user_id>."""
    if not await is_admin(update, context):
        return
    message = update.effective_message
    if message is None:
        return
    user_id = parse_target_user_id(context.args)
    if user_id is None:
        await message.reply_text("Usage: /remove_premium <user_id>")
        return
    async with get_user_lock(context):
        record = get_user_record(context, user_id)
        record["premium_until"] = None
        record["premium_lifetime"] = False
        save_user_state(context)
    await message.reply_text(f"Premium removed for {user_id}.")


async def user_status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin command: /user_status <user_id>."""
    if not await is_admin(update, context):
        return
    message = update.effective_message
    if message is None:
        return
    user_id = parse_target_user_id(context.args)
    if user_id is None:
        await message.reply_text("Usage: /user_status <user_id>")
        return
    record = get_user_record(context, user_id)
    premium_until = parse_datetime(record.get("premium_until"))
    await message.reply_text(
        f"User: {user_id}\n"
        f"Premium: {'yes' if is_premium_user(record) else 'no'}\n"
        f"Premium until: "
        f"{'lifetime' if record.get('premium_lifetime') else (premium_until.isoformat() if premium_until else '—')}\n"
        f"Today's usage: {record.get('usage_count', 0)}\n"
        f"Successful referrals: {record.get('referral_count', 0)}"
    )


async def referrals_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin command: /referrals [user_id]."""
    if not await is_admin(update, context):
        return
    message = update.effective_message
    if message is None:
        return
    users = get_users(context)
    user_id = parse_target_user_id(context.args)
    if user_id is not None:
        record = users.get(str(user_id), default_user_record())
        await message.reply_text(
            f"User {user_id} successful referrals: "
            f"{record.get('referral_count', 0)}"
        )
        return
    total = sum(int(record.get("referral_count", 0)) for record in users.values())
    await message.reply_text(
        f"Tracked users: {len(users)}\nSuccessful referrals: {total}"
    )


async def approve_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin command: /approve <payment_id> [days]."""
    if not await is_admin(update, context):
        return
    message = update.effective_message
    if message is None or not context.args:
        if message:
            await message.reply_text("Usage: /approve <payment_id> [days]")
        return
    payment_id = context.args[0].upper()
    days = None
    if len(context.args) > 1:
        if not context.args[1].isdigit() or int(context.args[1]) <= 0:
            await message.reply_text("Days must be a positive integer.")
            return
        days = int(context.args[1])
    user_id = await approve_payment(payment_id, context, days)
    if user_id is None:
        await message.reply_text("Payment is missing or already processed.")
        return
    await message.reply_text(f"Payment {payment_id} approved for user {user_id}.")


async def reject_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin command: /reject <payment_id>."""
    if not await is_admin(update, context):
        return
    message = update.effective_message
    if message is None or not context.args:
        if message:
            await message.reply_text("Usage: /reject <payment_id>")
        return
    payment_id = context.args[0].upper()
    user_id = await reject_payment(payment_id, context)
    if user_id is None:
        await message.reply_text("Payment is missing or already processed.")
        return
    await message.reply_text(f"Payment {payment_id} rejected for user {user_id}.")


async def set_limit_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin command: /set_limit <number>."""
    if not await is_admin(update, context):
        return
    message = update.effective_message
    if message is None or not context.args or not context.args[0].isdigit():
        if message:
            await message.reply_text("Usage: /set_limit <positive_number>")
        return
    value = int(context.args[0])
    if value <= 0:
        await message.reply_text("The limit must be greater than zero.")
        return
    get_settings(context)["free_daily_limit"] = value
    save_settings(context)
    await message.reply_text(f"Free daily limit set to {value}.")


async def set_price_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin command: /set_price <7d|30d|lifetime> <display_price>."""
    if not await is_admin(update, context):
        return
    message = update.effective_message
    if message is None or len(context.args) < 2:
        if message:
            await message.reply_text(
                "Usage: /set_price <7d|30d|lifetime> <display_price>"
            )
        return
    plan_key = context.args[0].lower()
    if get_plan(get_settings(context), plan_key) is None:
        await message.reply_text("Plan must be 7d, 30d, or lifetime.")
        return
    price = " ".join(context.args[1:]).strip()
    if len(price) > 80:
        await message.reply_text("The displayed price is too long.")
        return
    get_settings(context)["plans"][plan_key]["price"] = price
    save_settings(context)
    await message.reply_text(f"{plan_key} Premium price set to: {price}")


async def set_upi_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin command: /set_upi <upi_id or payment details>."""
    if not await is_admin(update, context):
        return
    message = update.effective_message
    if message is None or not context.args:
        if message:
            await message.reply_text("Usage: /set_upi <upi_id or payment details>")
        return
    details = " ".join(context.args).strip()
    if len(details) > 200:
        await message.reply_text("UPI payment details are too long.")
        return
    get_settings(context)["upi_details"] = details
    save_settings(context)
    await message.reply_text("UPI payment details updated.")


async def admin_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the secure admin command menu."""
    if not await is_admin(update, context):
        return
    if update.effective_message:
        await update.effective_message.reply_text(
            "Admin commands:\n"
            "/set_price <7d|30d|lifetime> <price>\n"
            "/set_qr <local_path_or_https_url>\n"
            "/set_upi <upi_id_or_details>\n"
            "/approve <payment_id> [days]\n"
            "/reject <payment_id>\n"
            "/activate <user_id> [days]\n"
            "/remove_premium <user_id>\n"
            "/user_status <user_id>\n"
            "/referrals [user_id]\n"
            "/set_limit <number>\n"
            "/set_ad_link <url|off>\n"
            "/set_ad_message <message>"
        )


def is_http_url(value: str) -> bool:
    """Validate an ad URL without accepting arbitrary schemes."""
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def set_ad_link_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin command: /set_ad_link <https://...|off>."""
    if not await is_admin(update, context):
        return
    message = update.effective_message
    if message is None or not context.args:
        if message:
            await message.reply_text("Usage: /set_ad_link <https://...|off>")
        return
    value = context.args[0].strip()
    if value.lower() == "off":
        get_settings(context)["ad_link"] = ""
    elif is_http_url(value):
        get_settings(context)["ad_link"] = value
    else:
        await message.reply_text("Ad link must be an http or https URL.")
        return
    save_settings(context)
    await message.reply_text("Ad link updated.")


async def set_ad_message_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin command: /set_ad_message <message>."""
    if not await is_admin(update, context):
        return
    message = update.effective_message
    if message is None or not context.args:
        if message:
            await message.reply_text("Usage: /set_ad_message <message>")
        return
    value = " ".join(context.args).strip()
    if len(value) > 500:
        await message.reply_text("The ad message must be 500 characters or fewer.")
        return
    get_settings(context)["ad_message"] = value
    save_settings(context)
    await message.reply_text("Ad message updated.")


async def set_qr_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin command: /set_qr <local_path|https://...>."""
    if not await is_admin(update, context):
        return
    message = update.effective_message
    if message is None or not context.args:
        if message:
            await message.reply_text("Usage: /set_qr <local_path|https://...>")
        return
    value = context.args[0].strip()
    if not Path(value).is_file() and not is_http_url(value):
        await message.reply_text(
            "QR source must be an existing local image path or an http/https URL."
        )
        return
    get_settings(context)["qr_image_path"] = value
    save_settings(context)
    await message.reply_text("Premium QR source updated.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explain how to use the bot."""
    if update.effective_message:
        await update.effective_message.reply_text(
            "Open a video deep link or send `/start <code>` to receive a video.\n"
            "Use /premium for unlimited access or /refer to invite friends.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=premium_keyboard(),
        )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keep non-supported commands friendly and predictable."""
    if update.effective_message:
        await update.effective_message.reply_text(
            "Use a video deep link, /premium, or /refer.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=premium_keyboard(),
        )


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Log polling and handler failures without exposing secrets."""
    LOGGER.error("Unhandled Telegram bot error", exc_info=context.error)


def parse_admin_ids() -> set[int]:
    """Parse admin user IDs from the protected environment variable."""
    raw_admin_ids = os.getenv("TELEGRAM_ADMIN_IDS", "1045902686")
    admin_ids: set[int] = set()
    for raw_id in raw_admin_ids.split(","):
        value = raw_id.strip()
        if value.isdigit() and int(value) > 0:
            admin_ids.add(int(value))
    return admin_ids


def build_application(
    token: str,
    video_map: dict[str, dict[str, str]],
    video_map_path: Path,
    users: dict[str, dict[str, Any]],
    users_path: Path,
    payments: dict[str, dict[str, Any]],
    payments_path: Path,
    settings: dict[str, Any],
    settings_path: Path,
    admin_ids: set[int],
) -> Application:
    """Build the Telegram application and register handlers."""
    application = ApplicationBuilder().token(token).build()
    application.bot_data["video_map"] = video_map
    application.bot_data["video_map_path"] = video_map_path
    application.bot_data["video_map_lock"] = asyncio.Lock()
    application.bot_data["pending_subscriptions"] = {}
    application.bot_data["users"] = users
    application.bot_data["users_path"] = users_path
    application.bot_data["users_lock"] = asyncio.Lock()
    application.bot_data["payments"] = payments
    application.bot_data["payments_path"] = payments_path
    application.bot_data["payments_lock"] = asyncio.Lock()
    application.bot_data["pending_payment_plans"] = {}
    application.bot_data["pending_payment_uploads"] = {}
    application.bot_data["settings"] = settings
    application.bot_data["settings_path"] = settings_path
    application.bot_data["admin_ids"] = admin_ids
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("premium", premium_command))
    application.add_handler(CommandHandler("refer", refer_command))
    application.add_handler(CommandHandler("referrals", referrals_command))
    application.add_handler(CommandHandler("activate", activate_command))
    application.add_handler(CommandHandler("activate_premium", activate_command))
    application.add_handler(
        CommandHandler("remove_premium", remove_premium_command)
    )
    application.add_handler(CommandHandler("user_status", user_status_command))
    application.add_handler(CommandHandler("status", user_status_command))
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("reject", reject_command))
    application.add_handler(CommandHandler("set_limit", set_limit_command))
    application.add_handler(CommandHandler("set_price", set_price_command))
    application.add_handler(CommandHandler("set_ad_link", set_ad_link_command))
    application.add_handler(
        CommandHandler("set_ad_message", set_ad_message_command)
    )
    application.add_handler(CommandHandler("set_qr", set_qr_command))
    application.add_handler(CommandHandler("set_upi", set_upi_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(
        CallbackQueryHandler(check_membership, pattern=f"^{CHECK_MEMBERSHIP_CALLBACK}$")
    )
    application.add_handler(
        CallbackQueryHandler(premium_callback, pattern=f"^{PREMIUM_CALLBACK}$")
    )
    application.add_handler(
        CallbackQueryHandler(refer_callback, pattern=f"^{REFER_CALLBACK}$")
    )
    application.add_handler(
        CallbackQueryHandler(
            payment_submit_callback,
            pattern=f"^{PAYMENT_SUBMIT_CALLBACK}.+",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            plan_callback,
            pattern=f"^{PLAN_CALLBACK_PREFIX}.+",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            ad_continue_callback,
            pattern=f"^{AD_CONTINUE_CALLBACK}$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            payment_approve_callback,
            pattern=f"^{PAYMENT_APPROVE_CALLBACK}.+",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            payment_reject_callback,
            pattern=f"^{PAYMENT_REJECT_CALLBACK}.+",
        )
    )
    application.add_handler(MessageHandler(filters.VIDEO, video_file_id))
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, image_message)
    )
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    """Start long polling and keep the bot running."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Add it as a Replit Secret before starting the bot."
        )

    video_map_path = Path(os.getenv("VIDEO_MAP_PATH", str(DEFAULT_VIDEO_MAP_PATH)))
    video_map = load_video_map(video_map_path)
    LOGGER.info("Loaded %d video deep-link payload(s)", len(video_map))

    users_path = Path(os.getenv("USERS_PATH", str(DEFAULT_USERS_PATH)))
    users = load_users(users_path)
    payments_path = Path(os.getenv("PAYMENTS_PATH", str(DEFAULT_PAYMENTS_PATH)))
    payments = load_payments(payments_path)
    settings_path = Path(os.getenv("SETTINGS_PATH", str(DEFAULT_SETTINGS_PATH)))
    settings_defaults: dict[str, Any] = {
        "free_daily_limit": integer_setting(
            "FREE_DAILY_LIMIT", DEFAULT_FREE_DAILY_LIMIT
        ),
        "premium_duration_days": integer_setting(
            "PREMIUM_DURATION_DAYS", DEFAULT_PREMIUM_DURATION_DAYS
        ),
        "referral_reward_days": integer_setting(
            "REFERRAL_REWARD_DAYS", DEFAULT_REFERRAL_REWARD_DAYS
        ),
        "qr_image_path": os.getenv("QR_IMAGE_PATH", "premium_qr.png"),
        "upi_details": os.getenv("UPI_ID", os.getenv("UPI_PAYMENT_DETAILS", "")),
        "ad_link": os.getenv("AD_LINK", ""),
        "ad_message": os.getenv("AD_MESSAGE", DEFAULT_AD_MESSAGE),
        "required_channel": os.getenv(
            "REQUIRED_CHANNEL", DEFAULT_REQUIRED_CHANNEL
        ),
        "required_channel_url": os.getenv(
            "REQUIRED_CHANNEL_URL", DEFAULT_REQUIRED_CHANNEL_URL
        ),
        "plans": {
            key: {
                **definition,
                "price": os.getenv(
                    env_name,
                    DEFAULT_PLAN_PRICES[key],
                ),
            }
            for key, definition, env_name in (
                (
                    "7d",
                    DEFAULT_PLAN_DEFINITIONS["7d"],
                    "PREMIUM_PRICE_7_DAYS",
                ),
                (
                    "30d",
                    DEFAULT_PLAN_DEFINITIONS["30d"],
                    "PREMIUM_PRICE_30_DAYS",
                ),
                (
                    "lifetime",
                    DEFAULT_PLAN_DEFINITIONS["lifetime"],
                    "PREMIUM_PRICE_LIFETIME",
                ),
            )
        },
    }
    settings = load_settings(settings_path, settings_defaults)
    if settings["ad_link"] and not is_http_url(str(settings["ad_link"])):
        raise RuntimeError("AD_LINK must be an http or https URL.")
    admin_ids = parse_admin_ids()
    LOGGER.info("Loaded %d persistent user record(s)", len(users))
    LOGGER.info("Loaded %d payment record(s)", len(payments))
    if not admin_ids:
        LOGGER.warning("No TELEGRAM_ADMIN_IDS are configured")

    application = build_application(
        token,
        video_map,
        video_map_path,
        users,
        users_path,
        payments,
        payments_path,
        settings,
        settings_path,
        admin_ids,
    )
    LOGGER.info("Telegram video bot is starting")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
