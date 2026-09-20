from __future__ import annotations

import re

from telegram import Chat, User

from funko_deal_bot.config import Settings

_SLASH_CMD_RE = re.compile(
    r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(?:\s|$)",
    re.I,
)


def normalize_username(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().lstrip("@").lower()


def allowed_usernames(settings: Settings) -> set[str]:
    raw = settings.telegram_allowed_usernames or ""
    return {name for part in raw.split(",") if (name := normalize_username(part))}


def owner_user_id(settings: Settings) -> str:
    return str(settings.telegram_owner_chat_id or "").strip()


def user_is_allowed(user: User | None, settings: Settings) -> bool:
    if user is None:
        return False
    names = allowed_usernames(settings)
    username = normalize_username(user.username)
    if username and username in names:
        return True
    owner_id = owner_user_id(settings)
    if owner_id and str(user.id) == owner_id:
        return True
    return False


def is_private_chat(chat: Chat | None) -> bool:
    return bool(chat) and chat.type == Chat.PRIVATE


def is_group_chat(chat: Chat | None) -> bool:
    return bool(chat) and chat.type in (Chat.GROUP, Chat.SUPERGROUP)


def parse_slash_command(text: str) -> tuple[str, str | None] | None:
    """`/scan`, `/scan@BotName`, `/check@BotName https://...` → (name, mention)."""
    match = _SLASH_CMD_RE.match((text or "").strip())
    if not match:
        return None
    return match.group(1).lower(), match.group(2)


def slash_command_for_us(text: str, bot_username: str | None) -> str | None:
    """Command name if this message is for us. Drops `/scan@OtherBot`."""
    parsed = parse_slash_command(text)
    if not parsed:
        return None
    name, mention = parsed
    if mention:
        us = normalize_username(bot_username)
        them = normalize_username(mention)
        if us and them != us:
            return None
    return name


def command_chat_allowed(chat: Chat | None, bound_group_id: str, command: str) -> bool:
    """Owner already passed username ACL. Limit group commands to the bound chat."""
    if chat is None:
        return False
    if is_private_chat(chat):
        return True
    if not is_group_chat(chat):
        return False
    name = command.lstrip("/").lower()
    if name in {"start", "bind", "unbind"}:
        return True
    return bool(bound_group_id) and str(chat.id) == str(bound_group_id)
