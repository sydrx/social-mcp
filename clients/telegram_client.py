"""
clients/telegram_client.py

Thin async wrapper around Telethon that exposes the operations social-mcp
needs: listing unread dialogs, fetching chat history, and sending messages.

Telethon is natively asyncio-based, so this wrapper is used directly from
the MCP server's async tool handlers without a thread bridge.

Authentication note:
    The first login requires an interactive code (and possibly 2FA password).
    Run `python setup_auth.py --telegram` once, from a normal terminal, to
    generate the .session file referenced by TelegramConfig.session_path.
    The MCP server itself assumes an already-authorized session and will
    raise a clear error if it is not.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from telethon import TelegramClient
from telethon.errors import RPCError, ChatAdminRequiredError
from telethon.tl.custom.dialog import Dialog
from telethon.tl.custom.message import Message
from telethon.tl.functions.contacts import BlockRequest, UnblockRequest, GetBlockedRequest
from telethon.tl.functions.messages import (
    AddChatUserRequest,
    CreateChatRequest,
    DeleteChatRequest,
    DeleteChatUserRequest,
    DeleteHistoryRequest,
    EditChatTitleRequest,
)
from telethon.tl.functions.channels import CreateChannelRequest, InviteToChannelRequest, EditBannedRequest, DeleteChannelRequest, LeaveChannelRequest
from telethon.tl.types import Chat, Channel, ChatBannedRights, InputUserSelf
from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import TelegramConfig

logger = logging.getLogger("social_mcp.telegram")


class TelegramNotAuthorizedError(RuntimeError):
    """Raised when the stored session is missing or not authorized."""


class TelegramClientWrapper:
    """
    Wraps a single Telethon TelegramClient instance with connection reuse.

    Instantiate once per process and call `connect()` before first use;
    the MCP server keeps this object alive for the server's lifetime.
    """

    def __init__(self, cfg: TelegramConfig) -> None:
        self._cfg = cfg
        self._client = TelegramClient(cfg.session_path, cfg.api_id, cfg.api_hash)
        self._connected = False
        self._created_chats: dict[int, Any] = {}  # cache created chat entities by id

    async def connect(self) -> None:
        if self._connected:
            return
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise TelegramNotAuthorizedError(
                "Telegram session is not authorized. Run 'python setup_auth.py "
                "--telegram' first to complete the interactive login."
            )
        self._connected = True
        await self._client.get_dialogs(limit=5)
        logger.info("Telegram client connected, authorized, and cache warmed.")

    async def disconnect(self) -> None:
        if self._connected:
            await self._client.disconnect()
            self._connected = False

    async def get_unread_messages(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return up to `limit` most-recent unread messages across all dialogs."""
        await self.connect()
        results: list[dict[str, Any]] = []

        try:
            async for dialog in self._client.iter_dialogs():
                dialog: Dialog
                if dialog.unread_count <= 0:
                    continue

                # Pull the most recent unread messages for this dialog.
                per_dialog_limit = min(dialog.unread_count, limit)
                async for msg in self._client.iter_messages(
                    dialog.id, limit=per_dialog_limit
                ):
                    msg: Message
                    if not msg.text:
                        continue
                    sender = await msg.get_sender()
                    sender_name = _extract_display_name(sender)
                    sender_username = getattr(sender, "username", None)

                    results.append(
                        {
                            "message_id": f"tg_{msg.id}",
                            "platform": "telegram",
                            "sender_id": msg.sender_id,
                            "sender_name": sender_name,
                            "sender_username": sender_username,
                            "timestamp": _to_iso(msg.date),
                            "text": msg.text,
                            "chat_type": "private" if dialog.is_user else "group",
                        }
                    )

                    if len(results) >= limit:
                        return results
        except RPCError as exc:
            logger.exception("Telegram RPC error while fetching unread messages")
            raise

        return results

    async def get_chat_history(self, target_id: str | int, limit: int = 10) -> list[dict[str, Any]]:
        """Return the last `limit` messages from a specific chat, oldest last."""
        await self.connect()
        history: list[dict[str, Any]] = []
        target = self._resolve_entity(target_id)

        async for msg in self._client.iter_messages(target, limit=limit):
            msg: Message
            if not msg.text:
                continue
            sender = await msg.get_sender()
            history.append(
                {
                    "message_id": f"tg_{msg.id}",
                    "platform": "telegram",
                    "sender_id": msg.sender_id,
                    "sender_name": _extract_display_name(sender),
                    "sender_username": getattr(sender, "username", None),
                    "timestamp": _to_iso(msg.date),
                    "text": msg.text,
                    "is_outgoing": msg.out,
                }
            )

        return history

    async def send_message(self, target_id: str | int, text: str) -> dict[str, Any]:
        """Send `text` to `target_id` (chat id, user id, or @username)."""
        await self.connect()
        try:
            target = self._resolve_entity(target_id)
            sent: Message = await self._client.send_message(target, text)
        except RPCError as exc:
            logger.exception("Telegram send_message failed")
            return {
                "success": False,
                "platform": "telegram",
                "error": str(exc),
            }

        return {
            "success": True,
            "platform": "telegram",
            "message_id": f"tg_{sent.id}",
            "timestamp": _to_iso(sent.date),
        }

    async def edit_message(self, chat_id: str | int, message_id: str | int, text: str) -> dict[str, Any]:
        await self.connect()
        try:
            chat = self._resolve_entity(chat_id)
            raw_id = int(str(message_id).removeprefix("tg_"))
            edited = await self._client.edit_message(chat, raw_id, text)
            return {
                "success": True,
                "platform": "telegram",
                "message_id": f"tg_{edited.id}",
                "text": text,
                "timestamp": _to_iso(edited.date),
            }
        except RPCError as exc:
            logger.exception("Telegram edit_message failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def delete_message(self, chat_id: str | int, message_id: str | int) -> dict[str, Any]:
        """Delete a message by its ID in a given chat."""
        await self.connect()
        try:
            chat = self._resolve_entity(chat_id)
            raw_id = int(str(message_id).removeprefix("tg_"))
            await self._client.delete_messages(chat, [raw_id])
            return {"success": True, "platform": "telegram", "action": "deleted", "message_id": message_id}
        except RPCError as exc:
            logger.exception("Telegram delete_message failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def leave_chat(self, chat_id: str | int) -> dict[str, Any]:
        """Leave a group/channel WITHOUT touching other members.

        Nobody gets kicked — just our own exit. For private dialogs
        this is a no-op (use delete_chat instead).
        """
        await self.connect()
        try:
            target = await self._resolve_any_entity(chat_id)
            if isinstance(target, Chat):
                await self._client(DeleteChatUserRequest(chat_id=target.id, user_id=InputUserSelf()))
            elif isinstance(target, Channel):
                await self._client(LeaveChannelRequest(
                    channel=await self._client.get_input_entity(target)
                ))
            else:
                return {"success": False, "platform": "telegram",
                        "error": "leave_chat applies to groups/channels only; "
                                 "use delete_chat for private dialogs."}
            # Purge our local dialog copy so the exit looks clean,
            # exactly like leaving from the official app.
            try:
                await self._client(DeleteHistoryRequest(
                    peer=await self._client.get_input_entity(target), max_id=0
                ))
            except Exception:  # noqa: BLE001 - cosmetic step, never fail the exit
                pass
            return {"success": True, "platform": "telegram", "action": "left"}
        except RPCError as exc:
            logger.exception("Telegram leave_chat failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def delete_chat(self, chat_id: str | int) -> dict[str, Any]:
        """FULLY delete / dissolve a chat ("удалить чат" = exactly this).

        - Basic group -> dissolution ritual: kick EVERY member, then leave,
          then revoke-purge the lingering empty dialog.
        - Owned channel/supergroup -> hard delete (DeleteChannelRequest),
          works even after we've left it.
        - Other channels/supergroups -> leave + purge own copy.
        - Private dialog -> revoke-delete the conversation.

        To exit WITHOUT touching members use leave_chat().
        """
        await self.connect()
        try:
            me_id = (await self._client.get_me()).id
            target = await self._resolve_any_entity(chat_id)

            if isinstance(target, Chat):
                # Fast path: admins/creator can nuke the whole basic group
                # in ONE request (this is what official clients send).
                try:
                    await self._client(DeleteChatRequest(chat_id=target.id))
                    return {"success": True, "platform": "telegram", "action": "group_deleted"}
                except ChatAdminRequiredError:
                    pass  # no admin rights -> dissolve manually below

                # --- dissolution ritual (fallback) ---
                kicked: list[int] = []
                kick_errors: list[dict[str, Any]] = []
                async for u in self._client.iter_participants(target):
                    if u.id == me_id:
                        continue
                    try:
                        await self._client(DeleteChatUserRequest(chat_id=target.id, user_id=u.id))
                        kicked.append(u.id)
                    except RPCError as exc:
                        kick_errors.append({"user_id": u.id, "error": str(exc)})
                await self._client(DeleteChatUserRequest(chat_id=target.id, user_id=InputUserSelf()))
                # Purge the leftover empty dialog (group may linger as creator).
                try:
                    await self._client.delete_dialog(target, revoke=True)
                except Exception:  # noqa: BLE001 - already dissolved server-side
                    pass
                result: dict[str, Any] = {
                    "success": True, "platform": "telegram",
                    "action": "group_dissolved", "kicked": kicked,
                }
                if kick_errors:
                    result["kick_errors"] = kick_errors
                return result

            if isinstance(target, Channel) and getattr(target, "creator", False):
                # Owned megagroup/channel: full deletion, membership not required.
                await self._client(DeleteChannelRequest(
                    channel=await self._client.get_input_entity(target)
                ))
                return {"success": True, "platform": "telegram", "action": "channel_deleted"}

            await self._client.delete_dialog(target, revoke=True)
            return {"success": True, "platform": "telegram", "action": "dialog_deleted"}
        except RPCError as exc:
            logger.exception("Telegram delete_chat failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}
        except RPCError as exc:
            logger.exception("Telegram delete_chat failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def block_user(self, user_id: str | int) -> dict[str, Any]:
        """Block a user by their ID."""
        await self.connect()
        try:
            entity = await self._client.get_input_entity(user_id)
            await self._client(BlockRequest(id=entity))
            return {"success": True, "platform": "telegram", "action": "blocked", "user_id": str(user_id)}
        except RPCError as exc:
            logger.exception("Telegram block_user failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def unblock_user(self, user_id: str | int) -> dict[str, Any]:
        """Unblock a user."""
        await self.connect()
        try:
            entity = await self._client.get_input_entity(user_id)
            await self._client(UnblockRequest(id=entity))
            return {"success": True, "platform": "telegram", "action": "unblocked", "user_id": str(user_id)}
        except RPCError as exc:
            logger.exception("Telegram unblock_user failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def get_blocked_users(self) -> list[dict[str, Any]]:
        await self.connect()
        try:
            result = await self._client(GetBlockedRequest(offset=0, limit=100))
            users = []
            for peer_blocked in result.blocked:
                user_id = peer_blocked.peer_id.user_id
                try:
                    entity = await self._client.get_entity(user_id)
                    name = _extract_display_name(entity)
                    username = getattr(entity, "username", None)
                except Exception:
                    name = f"user_{user_id}"
                    username = None
                users.append({"user_id": user_id, "name": name, "username": username})
            return users
        except RPCError as exc:
            logger.exception("Telegram get_blocked_users failed")
            return []

    async def create_group(self, title: str, user_ids: list[str | int]) -> dict[str, Any]:
        """Create a basic group with given users."""
        await self.connect()
        try:
            users = [await self._client.get_input_entity(uid) for uid in user_ids]
            result = await self._client(CreateChatRequest(users=users, title=title))
            # Telethon >= 1.44 returns messages.InvitedUsers (attrs: updates,
            # missing_invitees) instead of a bare Updates object.
            chats = getattr(result, "chats", None)
            if not chats:
                chats = getattr(getattr(result, "updates", None), "chats", [])
            chat = chats[0]
            self._created_chats[chat.id] = chat
            return {
                "success": True,
                "platform": "telegram",
                "action": "group_created",
                "chat_id": chat.id,
                "title": title,
            }
        except RPCError as exc:
            logger.exception("Telegram create_group failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def create_supergroup(self, title: str, description: str = "", megagroup: bool = True) -> dict[str, Any]:
        """Create a supergroup or channel (megagroup=True -> group, False -> channel)."""
        await self.connect()
        try:
            result = await self._client(CreateChannelRequest(
                title=title,
                about=description,
                megagroup=megagroup,
            ))
            chat = result.chats[0]
            self._created_chats[chat.id] = chat
            full_id = f"-100{chat.id}"
            return {
                "success": True,
                "platform": "telegram",
                "action": "supergroup_created",
                "chat_id": chat.id,
                "full_chat_id": full_id,
                "title": title,
                "megagroup": megagroup,
            }
        except RPCError as exc:
            logger.exception("Telegram create_supergroup failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def add_user_to_group(self, group_id: str | int, user_id: str | int) -> dict[str, Any]:
        await self.connect()
        try:
            user = await self._client.get_input_entity(user_id)
            entity = await self._get_group_entity(group_id)

            if isinstance(entity, Chat):
                # Basic group: AddChatUserRequest needs the raw int chat id.
                await self._client(AddChatUserRequest(chat_id=entity.id, user_id=user, fwd_limit=100))
            elif isinstance(entity, Channel):
                # Supergroup/channel: invite via channels.inviteToChannel.
                channel = await self._client.get_input_entity(entity)
                await self._client(InviteToChannelRequest(channel=channel, users=[user]))
            else:
                return {"success": False, "platform": "telegram",
                        "error": f"Unsupported group type: {type(entity).__name__}"}

            return {"success": True, "platform": "telegram", "action": "user_added", "user_id": str(user_id)}
        except RPCError as exc:
            logger.exception("Telegram add_user_to_group failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def invite_to_channel(self, channel_id: str | int, user_ids: list[str | int]) -> dict[str, Any]:
        await self.connect()
        try:
            channel = await self._client.get_input_entity(await self._get_group_entity(channel_id))
            users = [await self._client.get_input_entity(uid) for uid in user_ids]
            await self._client(InviteToChannelRequest(channel=channel, users=users))
            return {"success": True, "platform": "telegram", "action": "invited", "users": [str(u) for u in user_ids]}
        except RPCError as exc:
            logger.exception("Telegram invite_to_channel failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def remove_user_from_group(self, group_id: str | int, user_id: str | int) -> dict[str, Any]:
        await self.connect()
        try:
            user = await self._client.get_input_entity(user_id)
            entity = await self._get_group_entity(group_id)

            if isinstance(entity, Chat):
                # Basic group: kick via messages.deleteChatUser (needs raw int id).
                await self._client(DeleteChatUserRequest(chat_id=entity.id, user_id=user))
            elif isinstance(entity, Channel):
                # Supergroup/channel: ban (EditBannedRequest requires InputChannel).
                channel = await self._client.get_input_entity(entity)
                await self._client(EditBannedRequest(
                    channel=channel,
                    participant=user,
                    banned_rights=ChatBannedRights(
                        until_date=None, view_messages=True, send_messages=True,
                        send_media=True, send_stickers=True, send_gifs=True,
                        send_games=True, send_inline=True, embed_links=True,
                    ),
                ))
            else:
                return {"success": False, "platform": "telegram",
                        "error": f"Unsupported group type: {type(entity).__name__}"}

            return {"success": True, "platform": "telegram", "action": "user_removed", "user_id": str(user_id)}
        except RPCError as exc:
            logger.exception("Telegram remove_user_from_group failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def _get_group_entity(self, group_id: str | int) -> Any:
        """Resolve a group/channel id to its entity, trying all Telegram
        id-marking conventions (bare / -chat / -100channel)."""
        raw = str(group_id).strip()
        candidates: list[Any] = [raw]
        # Normalize any marked form to bare digits first.
        s = raw[4:] if raw.startswith("-100") else (raw[1:] if raw.startswith("-") else raw)
        if s.isdigit():
            n = int(s)
            candidates += [-n, int(f"-100{n}")]
        last_exc: Exception | None = None
        for cand in candidates:
            try:
                entity = await self._client.get_entity(cand)
                if isinstance(entity, (Chat, Channel)):
                    return entity
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        raise ValueError(f"Cannot resolve group '{group_id}': {last_exc}")

    async def _resolve_any_entity(self, chat_id: str | int) -> Any:
        """Resolve any chat id (user / basic group / channel / @username)
        via Telegram's own entity cache, trying all id-marking conventions."""
        raw = str(chat_id).strip()
        # Normalize any marked form to bare digits first.
        s = raw[4:] if raw.startswith("-100") else (raw[1:] if raw.startswith("-") else raw)
        if not s.isdigit():
            return await self._client.get_entity(self._resolve_entity(raw))
        n = int(s)
        last_exc: Exception | None = None
        for cand in (n, -n, int(f"-100{n}")):
            try:
                return await self._client.get_entity(cand)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        raise ValueError(f"Cannot resolve chat '{chat_id}': {last_exc}")

    def _resolve_entity(self, chat_id: str | int) -> Any:
        raw = str(chat_id).strip()
        if raw.startswith("@") or raw.startswith("+"):
            return raw
        if raw.lstrip("-").isdigit():
            if raw.startswith("-100"):
                chat_id_int = int(raw[4:])
            elif raw.startswith("-"):
                chat_id_int = int(raw[1:])
            else:
                chat_id_int = int(raw)
            cached = self._created_chats.get(chat_id_int)
            if cached is not None:
                return cached
            if raw.startswith("-100"):
                return InputPeerChannel(channel_id=chat_id_int, access_hash=0)
            if raw.startswith("-"):
                return InputPeerChat(chat_id=chat_id_int)
            if chat_id_int > 0:
                return InputPeerUser(user_id=chat_id_int, access_hash=0)
        return chat_id


def _extract_display_name(sender: Any) -> str:
    if sender is None:
        return "unknown"
    first = getattr(sender, "first_name", None) or ""
    last = getattr(sender, "last_name", None) or ""
    title = getattr(sender, "title", None)  # channels/groups
    name = (title or f"{first} {last}").strip()
    return name or getattr(sender, "username", None) or "unknown"


def _to_iso(dt: datetime | None) -> str:
    if dt is None:
        return datetime.now(timezone.utc).isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
