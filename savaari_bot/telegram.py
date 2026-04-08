"""Tiny Telegram Bot API client built on httpx.

Why not python-telegram-bot? It's a heavy dependency that doubles the
PyInstaller binary size and trips antivirus heuristics more often than raw
httpx. We only need four methods:

  - sendMessage           with reply_markup for inline keyboards
  - editMessageText       to edit a sent alert in place after action
  - answerCallbackQuery   to dismiss the spinner on the user's button tap
  - getUpdates            long-poll loop for receiving callback queries

That's <150 lines.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger("savaari_bot.telegram")


CallbackHandler = Callable[["CallbackQuery"], Awaitable[None]]
MessageHandler = Callable[["IncomingMessage"], Awaitable[None]]


@dataclass
class CallbackQuery:
    id: str
    from_user_id: int
    message_id: int
    chat_id: int
    data: str


@dataclass
class IncomingMessage:
    message_id: int
    chat_id: int
    from_user_id: int
    text: str


@dataclass
class TelegramBot:
    token: str
    chat_id: str
    timeout_s: float = 35.0
    on_callback: CallbackHandler | None = None
    on_message: MessageHandler | None = None
    _offset: int = 0
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def base(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    def stop(self) -> None:
        self._stop.set()

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(f"{self.base}/{method}", json=payload)
            resp.raise_for_status()
            data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {data}")
        return data["result"]

    async def send_message(
        self,
        text: str,
        *,
        buttons: list[list[tuple[str, str]]] | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in row]
                    for row in buttons
                ]
            }
        return await self._post("sendMessage", payload)

    async def edit_message_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        *,
        buttons: list[list[tuple[str, str]]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons is not None:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in row]
                    for row in buttons
                ]
            }
        try:
            await self._post("editMessageText", payload)
        except RuntimeError as e:
            # "message is not modified" is harmless and very common when our
            # poller re-renders the same alert text.
            if "not modified" not in str(e):
                raise

    async def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        await self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def run_polling(self) -> None:
        """Long-poll getUpdates and dispatch callback queries.

        We start with offset=-1 to consume any backlog from before the bot
        started, then advance from there.
        """
        log.info("telegram polling started")
        # Bootstrap: discard backlog so we don't replay stale presses.
        try:
            updates = await self._get_updates(timeout=0, offset=-1)
            for u in updates:
                self._offset = u["update_id"] + 1
        except Exception:
            log.exception("telegram bootstrap failed (continuing)")

        while not self._stop.is_set():
            try:
                updates = await self._get_updates(timeout=25, offset=self._offset)
            except httpx.ReadTimeout:
                continue
            except Exception:
                log.exception("telegram getUpdates failed; backing off")
                await self._sleep(5.0)
                continue

            for u in updates:
                self._offset = u["update_id"] + 1
                cbq = u.get("callback_query")
                if cbq and self.on_callback:
                    try:
                        await self.on_callback(self._parse_cbq(cbq))
                    except Exception:
                        log.exception("callback handler crashed")
                msg = u.get("message")
                if msg and self.on_message:
                    parsed = self._parse_msg(msg)
                    if parsed is not None:
                        try:
                            await self.on_message(parsed)
                        except Exception:
                            log.exception("message handler crashed")

    async def _get_updates(self, *, timeout: int, offset: int) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.get(
                f"{self.base}/getUpdates",
                params={
                    "timeout": timeout,
                    "offset": offset,
                    "allowed_updates": '["callback_query","message"]',
                },
            )
            resp.raise_for_status()
            data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"getUpdates failed: {data}")
        return data["result"]

    @staticmethod
    def _parse_cbq(cbq: dict[str, Any]) -> CallbackQuery:
        return CallbackQuery(
            id=str(cbq["id"]),
            from_user_id=int(cbq["from"]["id"]),
            message_id=int(cbq["message"]["message_id"]),
            chat_id=int(cbq["message"]["chat"]["id"]),
            data=str(cbq.get("data") or ""),
        )

    @staticmethod
    def _parse_msg(msg: dict[str, Any]) -> "IncomingMessage | None":
        text = msg.get("text")
        if not text:
            return None
        try:
            return IncomingMessage(
                message_id=int(msg["message_id"]),
                chat_id=int(msg["chat"]["id"]),
                from_user_id=int(msg["from"]["id"]),
                text=str(text),
            )
        except (KeyError, TypeError, ValueError):
            return None

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
