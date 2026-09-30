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
import re
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
    GetFullChatRequest,
)
from telethon.tl.functions.channels import CreateChannelRequest, InviteToChannelRequest, EditBannedRequest, DeleteChannelRequest, LeaveChannelRequest
from telethon.tl.types import Chat, Channel, ChatBannedRights, InputUserSelf

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
        self._user_cache: dict[int, Any] = {}  # resolved users, survives dialog deletion

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

    async def mark_read(self, chat_id: str | int | None = None) -> dict[str, Any]:
        """Clear Telegram's unread flag for one dialog, or for all of them.

        Reading messages through get_unread_messages deliberately does NOT
        do this; the flag only moves when the boss asks for it.
        """
        await self.connect()
        cleared: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        try:
            if chat_id is not None:
                targets = [await self._resolve_any_entity(chat_id)]
            else:
                targets = [
                    d.entity async for d in self._client.iter_dialogs()
                    if d.unread_count > 0
                ]

            for entity in targets:
                title = getattr(entity, "title", None) or _extract_display_name(entity)
                try:
                    await self._client.send_read_acknowledge(entity)
                    cleared.append({"chat_id": str(getattr(entity, "id", "")), "title": title})
                except RPCError as exc:
                    errors.append({
                        "chat_id": str(getattr(entity, "id", "")),
                        "title": title,
                        "error": str(exc),
                    })

            return {
                "success": not errors,
                "platform": "telegram",
                "action": "marked_read",
                "cleared": cleared,
                "errors": errors,
            }
        except (RPCError, ValueError) as exc:
            logger.exception("Telegram mark_read failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}

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
                    sender = await msg.get_sender()
                    sender_name = _extract_display_name(sender)
                    sender_username = getattr(sender, "username", None)
                    service = _service_action(msg)

                    results.append(
                        {
                            "message_id": f"tg_{msg.id}",
                            "platform": "telegram",
                            "chat_id": str(dialog.id),
                            "sender_id": msg.sender_id,
                            "sender_name": sender_name,
                            "sender_username": sender_username,
                            "timestamp": _to_iso(msg.date),
                            "text": _message_text(msg),
                            "has_media": _has_media(msg),
                            "is_service": service is not None,
                            "service_action": service,
                            "chat_type": "private" if dialog.is_user else "group",
                        }
                    )

                    if len(results) >= limit:
                        return results
        except RPCError as exc:
            logger.exception("Telegram RPC error while fetching unread messages")
            raise

        return results

    async def get_chat_history(
        self,
        target_id: str | int,
        limit: int = 10,
        offset_id: str | int | None = None,
    ) -> list[dict[str, Any]]:
        """Return up to `limit` messages from a chat, oldest last.

        `offset_id` pages backwards: pass the OLDEST message_id you already
        have to continue into older history.
        """
        await self.connect()
        history: list[dict[str, Any]] = []
        target = await self._resolve_entity(target_id)

        kwargs: dict[str, Any] = {"limit": limit}
        if offset_id is not None:
            kwargs["offset_id"] = int(str(offset_id).removeprefix("tg_"))

        async for msg in self._client.iter_messages(target, **kwargs):
            msg: Message
            sender = await msg.get_sender()
            service = _service_action(msg)
            history.append(
                {
                    "message_id": f"tg_{msg.id}",
                    "platform": "telegram",
                    "sender_id": msg.sender_id,
                    "sender_name": _extract_display_name(sender),
                    "sender_username": getattr(sender, "username", None),
                    "timestamp": _to_iso(msg.date),
                    "text": _message_text(msg),
                    "has_media": _has_media(msg),
                    "is_service": service is not None,
                    "service_action": service,
                    "is_outgoing": msg.out,
                }
            )

        return history

    async def list_dialogs(
        self,
        limit: int = 50,
        only_groups: bool = False,
        only_unread: bool = False,
    ) -> list[dict[str, Any]]:
        """List conversations so their ids can be used as tool targets."""
        await self.connect()
        out: list[dict[str, Any]] = []

        async for dialog in self._client.iter_dialogs(limit=limit):
            dialog: Dialog
            entity = dialog.entity
            is_user = dialog.is_user
            is_group = dialog.is_group or dialog.is_channel

            if only_groups and is_user:
                continue
            if only_unread and dialog.unread_count <= 0:
                continue

            if is_user:
                chat_type = "private"
            elif getattr(entity, "broadcast", False):
                chat_type = "channel"
            else:
                chat_type = "group"

            last = None
            try:
                async for msg in self._client.iter_messages(dialog, limit=1):
                    last = msg
                    break
            except RPCError:
                pass

            out.append(
                {
                    "chat_id": str(dialog.id),
                    "platform": "telegram",
                    "title": dialog.name or "",
                    "chat_type": chat_type,
                    "is_group": is_group,
                    "unread_count": dialog.unread_count,
                    "participants_count": getattr(entity, "participants_count", None),
                    "last_message_id": f"tg_{last.id}" if last else None,
                    "last_message_at": _to_iso(last.date) if last else None,
                    "last_message_preview": _message_text(last)[:120] if last else None,
                }
            )
            if len(out) >= limit:
                break

        return out

    async def get_chat_info(self, target_id: str | int) -> dict[str, Any]:
        """Describe a chat and, where permitted, list its members with roles."""
        await self.connect()
        entity = await self._resolve_any_entity(target_id)
        info: dict[str, Any] = {
            "chat_id": str(getattr(entity, "id", target_id)),
            "platform": "telegram",
            "title": getattr(entity, "title", None)
            or _extract_display_name(entity),
            "type": type(entity).__name__,
            "participants_count": getattr(entity, "participants_count", None),
            "is_creator": getattr(entity, "creator", None),
        }

        if isinstance(entity, Chat):
            # Basic groups return bare User objects from iter_participants,
            # with no role info; the creator comes from the full-chat request.
            creator_id: int | None = None
            try:
                full = await self._client(GetFullChatRequest(chat_id=entity.id))
                creator_id = getattr(full.full_chat.participants, "creator", None)
            except RPCError as exc:
                info["full_chat_error"] = str(exc)

            members: list[dict[str, Any]] = []
            try:
                async for u in self._client.iter_participants(entity):
                    members.append(
                        {
                            "user_id": str(u.id),
                            "name": _extract_display_name(u),
                            "username": getattr(u, "username", None),
                            "is_creator": u.id == creator_id,
                        }
                    )
                info["members"] = members
            except RPCError as exc:
                info["members_error"] = str(exc)
        elif isinstance(entity, Channel):
            try:
                part = await self._client.get_participants(entity)
                members = []
                for p in part:
                    members.append(
                        {
                            "user_id": str(p.id),
                            "name": _extract_display_name(p),
                            "username": getattr(p, "username", None),
                            "role": type(p.participant).__name__,
                            "is_admin": bool(getattr(p.participant, "admin_rights", None)),
                        }
                    )
                info["members"] = members
            except RPCError as exc:
                info["members_error"] = (
                    f"{exc} (listing members needs admin rights in a supergroup)"
                )

        return info

    async def send_message(
        self,
        target_id: str | int,
        text: str,
        reply_to_message_id: str | int | None = None,
        silent: bool = False,
    ) -> dict[str, Any]:
        """Send `text` to `target_id` (chat id, user id, or @username).

        `reply_to_message_id` makes it an actual threaded reply.
        `silent` sends without a notification sound.
        """
        await self.connect()
        try:
            target = await self._resolve_entity(target_id)
            kwargs: dict[str, Any] = {}
            if reply_to_message_id is not None:
                kwargs["reply_to"] = int(str(reply_to_message_id).removeprefix("tg_"))
            sent: Message = await self._client.send_message(
                target, text, silent=silent, **kwargs
            )
        except RPCError as exc:
            logger.exception("Telegram send_message failed")
            return {
                "success": False,
                "platform": "telegram",
                "error": str(exc),
            }
        except ValueError as exc:
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
            chat = await self._resolve_entity(chat_id)
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
        except ValueError as exc:
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def delete_message(self, chat_id: str | int, message_id: str | int) -> dict[str, Any]:
        """Delete a message by its ID in a given chat."""
        await self.connect()
        try:
            chat = await self._resolve_entity(chat_id)
            raw_id = int(str(message_id).removeprefix("tg_"))
            await self._client.delete_messages(chat, [raw_id])
            return {"success": True, "platform": "telegram", "action": "deleted", "message_id": message_id}
        except RPCError as exc:
            logger.exception("Telegram delete_message failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}
        except ValueError as exc:
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
        - Foreign supergroup/channel -> ban every member, then leave, then
          purge our own copy. The group itself survives (Telegram only lets
          the owner destroy it), but nobody is left holding a group we were in.
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
                    await self._purge_local_dialog(target)
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

            if isinstance(target, Channel):
                if getattr(target, "creator", False):
                    # Owned megagroup/channel: full deletion, membership not required.
                    await self._client(DeleteChannelRequest(
                        channel=await self._client.get_input_entity(target)
                    ))
                    await self._purge_local_dialog(target)
                    return {"success": True, "platform": "telegram", "action": "channel_deleted"}

                # Foreign megagroup: we cannot destroy it (owner-only), but we
                # can still ban every member so nobody inherits a group we
                # were in, then leave and purge our own copy.
                channel = await self._client.get_input_entity(target)
                banned: list[int] = []
                ban_errors: list[dict[str, Any]] = []
                async for u in self._client.iter_participants(target):
                    if u.id == me_id:
                        continue
                    try:
                        await self._client(EditBannedRequest(
                            channel=channel,
                            participant=await self._client.get_input_entity(u),
                            banned_rights=ChatBannedRights(
                                until_date=None, view_messages=True, send_messages=True,
                                send_media=True, send_stickers=True, send_gifs=True,
                                send_games=True, send_inline=True, embed_links=True,
                            ),
                        ))
                        banned.append(u.id)
                    except ChatAdminRequiredError:
                        return {
                            "success": False, "platform": "telegram",
                            "action": "no_admin_rights",
                            "error": "Not an admin: cannot remove members. "
                                     "Use leave_chat to exit without touching anyone.",
                        }
                    except RPCError as exc:
                        ban_errors.append({"user_id": u.id, "error": str(exc)})

                await self._client(LeaveChannelRequest(channel=channel))
                await self._purge_local_dialog(target)
                result: dict[str, Any] = {
                    "success": True, "platform": "telegram",
                    "action": "members_banned_and_left", "banned": banned,
                    "note": "Group still exists for its owner; Telegram only allows "
                            "the owner to destroy it. All members were removed.",
                }
                if ban_errors:
                    result["ban_errors"] = ban_errors
                return result

            await self._client.delete_dialog(target, revoke=True)
            return {"success": True, "platform": "telegram", "action": "dialog_deleted"}
        except RPCError as exc:
            logger.exception("Telegram delete_chat failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}
        except ValueError as exc:
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def _purge_local_dialog(self, target: Any) -> None:
        """Drop the chat from our own dialog list after it is destroyed.

        A deleted group is gone server-side, but our local dialog entry
        lingers and keeps showing up as an inaccessible chat. This is the
        same request official clients send when a chat is swiped away.
        Best-effort: never fail the deletion over a cosmetic step.
        """
        try:
            await self._client(
                DeleteHistoryRequest(
                    peer=await self._client.get_input_entity(target), max_id=0
                )
            )
        except Exception as exc:  # noqa: BLE001 - cosmetic step
            # Log the exception type only: %s on the entity would put the
            # chat id and title into the log file.
            logger.debug(
                "Local dialog purge skipped (%s)", type(exc).__name__, exc_info=True
            )

    async def block_user(self, user_id: str | int) -> dict[str, Any]:
        """Block a user by their ID."""
        await self.connect()
        try:
            entity = await self._client.get_input_entity(await self._resolve_entity(user_id))
            await self._client(BlockRequest(id=entity))
            return {"success": True, "platform": "telegram", "action": "blocked", "user_id": str(user_id)}
        except RPCError as exc:
            logger.exception("Telegram block_user failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}
        except ValueError as exc:
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def unblock_user(self, user_id: str | int) -> dict[str, Any]:
        """Unblock a user."""
        await self.connect()
        try:
            entity = await self._client.get_input_entity(await self._resolve_entity(user_id))
            await self._client(UnblockRequest(id=entity))
            return {"success": True, "platform": "telegram", "action": "unblocked", "user_id": str(user_id)}
        except RPCError as exc:
            logger.exception("Telegram unblock_user failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}
        except ValueError as exc:
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
            users = [
                await self._client.get_input_entity(await self._resolve_entity(uid))
                for uid in user_ids
            ]
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
        except ValueError as exc:
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
            user = await self._client.get_input_entity(await self._resolve_entity(user_id))
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
        except ValueError as exc:
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def invite_to_channel(self, channel_id: str | int, user_ids: list[str | int]) -> dict[str, Any]:
        await self.connect()
        try:
            channel = await self._client.get_input_entity(await self._get_group_entity(channel_id))
            users = [
                await self._client.get_input_entity(await self._resolve_entity(uid))
                for uid in user_ids
            ]
            await self._client(InviteToChannelRequest(channel=channel, users=users))
            return {"success": True, "platform": "telegram", "action": "invited", "users": [str(u) for u in user_ids]}
        except RPCError as exc:
            logger.exception("Telegram invite_to_channel failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}
        except ValueError as exc:
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def remove_user_from_group(self, group_id: str | int, user_id: str | int) -> dict[str, Any]:
        await self.connect()
        try:
            user = await self._client.get_input_entity(await self._resolve_entity(user_id))
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
        except ValueError as exc:
            return {"success": False, "platform": "telegram", "error": str(exc)}

    async def unban_user_from_group(self, group_id: str | int, user_id: str | int) -> dict[str, Any]:
        """Lift a ban in a supergroup.

        The counterpart of remove_user_from_group, which bans in supergroups.
        A basic group has no separate ban state, so it is a no-op there.
        """
        await self.connect()
        try:
            user = await self._client.get_input_entity(await self._resolve_entity(user_id))
            entity = await self._get_group_entity(group_id)

            if isinstance(entity, Chat):
                return {
                    "success": True, "platform": "telegram",
                    "action": "not_applicable",
                    "note": "Basic groups have no ban list; use add_user_to_group "
                            "to bring the member back.",
                }

            await self._client(EditBannedRequest(
                channel=await self._client.get_input_entity(entity),
                participant=user,
                banned_rights=ChatBannedRights(until_date=None),
            ))
            return {"success": True, "platform": "telegram", "action": "user_unbanned",
                    "user_id": str(user_id),
                    "note": "Ban lifted. The user must rejoin on their own; "
                            "re-inviting them is not possible until they do."}
        except RPCError as exc:
            logger.exception("Telegram unban_user_from_group failed")
            return {"success": False, "platform": "telegram", "error": str(exc)}
        except ValueError as exc:
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
            return await self._client.get_entity(await self._resolve_entity(raw))
        n = int(s)
        last_exc: Exception | None = None
        for cand in (n, -n, int(f"-100{n}")):
            try:
                return await self._client.get_entity(cand)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        raise ValueError(f"Cannot resolve chat '{chat_id}': {last_exc}")

    async def _resolve_entity(self, chat_id: str | int) -> Any:
        """Resolve a target to an entity carrying a valid access_hash.

        Telegram rejects hand-built InputPeer* objects with access_hash=0
        ("Invalid channel object"), so numeric ids must go through the
        session entity cache instead of being reconstructed by hand.
        """
        raw = str(chat_id).strip()
        if raw.startswith("@") or raw.startswith("+"):
            return raw
        if not raw.lstrip("-").isdigit():
            return chat_id

        # Normalize any marked form (-100x / -x / x) to the bare id.
        bare = raw[4:] if raw.startswith("-100") else (raw[1:] if raw.startswith("-") else raw)
        n = int(bare)

        cached = self._created_chats.get(n)
        if cached is not None:
            return cached

        cached_user = self._user_cache.get(n)
        if cached_user is not None:
            return cached_user

        last_exc: Exception | None = None
        for cand in (n, -n, int(f"-100{n}")):
            try:
                entity = await self._client.get_entity(cand)
                if n > 0:
                    self._user_cache[n] = entity  # survive dialog deletion
                return entity
            except Exception as exc:  # noqa: BLE001
                last_exc = exc

        # Telegram only accepts a user id together with its access_hash, so a
        # bare id whose dialog was deleted cannot be recovered by id alone.
        raise ValueError(
            f"Cannot resolve '{chat_id}': the entity is not in the session cache "
            f"(Telegram requires an access_hash it no longer knows). Pass an "
            f"@username instead - Telegram resolves that network-side."
        )


def _service_action(msg: Any) -> str | None:
    """Readable name of a service action (joins, renames, pins), else None.

    Service messages used to be dropped silently, which hid genuinely useful
    events like "X invited Y" or "group renamed to Z". They are now reported
    with `is_service: true` instead of vanishing.
    """
    action = getattr(msg, "action", None)
    if not action:
        return None
    name = type(action).__name__
    if name.endswith("Request"):
        name = name[: -len("Request")]
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name).lower()


def _message_text(msg: Any) -> str:
    """Text of a message, or a placeholder when it only carries media.

    Media-only messages used to be dropped entirely, which silently emptied
    photo-heavy chats. Now the message is reported with a placeholder so the
    caller can see that something was said.
    """
    if msg.text:
        return msg.text
    if getattr(msg, "photo", None):
        return "[photo]"
    if getattr(msg, "video", None):
        return "[video]"
    if getattr(msg, "voice", None):
        return "[voice]"
    if getattr(msg, "video_note", None):
        return "[video note]"
    if getattr(msg, "audio", None):
        return "[audio]"
    if getattr(msg, "sticker", None):
        return "[sticker]"
    if getattr(msg, "animation", None):
        return "[gif]"
    if getattr(msg, "document", None):
        return "[file]"
    if getattr(msg, "contact", None):
        return "[contact]"
    if getattr(msg, "location", None):
        return "[location]"
    if getattr(msg, "poll", None):
        return "[poll]"
    if getattr(msg, "game", None):
        return "[game]"
    service = _service_action(msg)
    if service:
        return f"[service: {service}]"
    return "[unsupported media]"


def _has_media(msg: Any) -> bool:
    return any(getattr(msg, a, None) for a in (
        "photo", "video", "voice", "video_note", "audio", "sticker",
        "animation", "document", "contact", "location", "poll", "game",
    ))


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
