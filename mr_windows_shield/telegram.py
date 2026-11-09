"""Telegram-бот для теста: тонкий клиент Bot API на httpx + long polling (getUpdates), без вебхуков.

Токен — секретная настройка плагина `telegram_token`, прокси (если api.telegram.org режется) — `telegram_proxy`.
Бот сам первым не пишет: человек один раз жмёт Start по ссылке t.me/<бот>?start=<код> из панели.
"""
import asyncio
import html
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

API = "https://api.telegram.org"
POLL_TIMEOUT = 25            # секунд держит getUpdates
RETRY_SECONDS = 15

logger = logging.getLogger(__name__)


class TelegramError(Exception):
    pass


@dataclass
class TgButton:
    label: str
    data: str                # callback_data, ≤ 64 байт


@dataclass
class TgUpdate:
    """Нужное из update: либо текст (/start …), либо нажатие кнопки."""
    chat_id: int
    user_id: int
    user_name: str
    text: str = ""
    callback_id: str = ""
    data: str = ""
    message_id: int = 0


def to_html(text: str) -> str:
    """Discord-разметка карточек теста → HTML Telegram: **жирный**, строки «-# …» мелким (курсив)."""
    out = []
    for line in html.escape(text, quote=False).split("\n"):
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        out.append(f"<i>{line[3:]}</i>" if line.startswith("-# ") else line)
    return "\n".join(out)


def keyboard(rows: list[list[TgButton]]) -> dict:
    return {"inline_keyboard": [[{"text": b.label, "callback_data": b.data} for b in row] for row in rows]}


def parse_update(raw: dict) -> TgUpdate | None:
    if query := raw.get("callback_query"):
        user = query.get("from") or {}
        message = query.get("message") or {}
        return TgUpdate(chat_id=(message.get("chat") or {}).get("id", user.get("id", 0)), user_id=user.get("id", 0),
                        user_name=_name(user), callback_id=query.get("id", ""), data=query.get("data", ""),
                        message_id=message.get("message_id", 0))
    message = raw.get("message")
    if message and (message.get("chat") or {}).get("type") == "private" and message.get("text"):
        user = message.get("from") or {}
        return TgUpdate(chat_id=message["chat"]["id"], user_id=user.get("id", 0), user_name=_name(user),
                        text=message["text"], message_id=message.get("message_id", 0))
    return None


def _name(user: dict) -> str:
    return user.get("username") and f"@{user['username']}" or user.get("first_name") or str(user.get("id", ""))


@dataclass
class TelegramBot:
    token: str
    proxy: str | None = None
    username: str = ""                   # из getMe — для ссылки привязки
    last_error: str = ""                 # показывается в панели
    handlers: list[Callable[[TgUpdate], Awaitable[None]]] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: dict) -> "TelegramBot | None":
        token = str(settings.get("telegram_token") or "").strip()
        if not token:
            return None
        return cls(token=token, proxy=str(settings.get("telegram_proxy") or "").strip() or None)

    def link(self, code: str) -> str | None:
        return f"https://t.me/{self.username}?start={code}" if self.username else None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=POLL_TIMEOUT + 15, proxy=self.proxy)

    async def call(self, method: str, client: httpx.AsyncClient | None = None, **params):
        own = client is None
        client = client or self._client()
        try:
            response = await client.post(f"{API}/bot{self.token}/{method}", json=params)
            body = response.json()
        except (httpx.HTTPError, ValueError) as e:
            # str(e) у httpx может содержать URL с токеном — не показываем
            raise TelegramError(f"нет связи с Telegram ({type(e).__name__})") from None
        finally:
            if own:
                await client.aclose()
        if not body.get("ok"):
            raise TelegramError(body.get("description") or f"HTTP {response.status_code}")
        return body["result"]

    async def send(self, chat_id: int, text: str, rows: list[list[TgButton]]) -> int:
        result = await self.call("sendMessage", chat_id=chat_id, text=to_html(text), parse_mode="HTML",
                                 reply_markup=keyboard(rows))
        return result["message_id"]

    async def edit(self, chat_id: int, message_id: int, text: str, rows: list[list[TgButton]]) -> None:
        try:
            await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=to_html(text),
                            parse_mode="HTML", reply_markup=keyboard(rows))
        except TelegramError as e:
            if "not modified" not in str(e):
                raise

    async def reply(self, chat_id: int, text: str) -> None:
        await self.call("sendMessage", chat_id=chat_id, text=to_html(text), parse_mode="HTML")

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError:
            pass                                    # кнопка «крутится» — не страшно

    async def _poll(self) -> None:
        offset = 0
        async with self._client() as client:
            while True:
                try:
                    if not self.username:
                        self.username = (await self.call("getMe", client))["username"]
                    updates = await self.call("getUpdates", client, offset=offset, timeout=POLL_TIMEOUT,
                                              allowed_updates=["message", "callback_query"])
                    self.last_error = ""
                except TelegramError as e:
                    if self.last_error != str(e):
                        logger.warning(f"Telegram: {e}")
                    self.last_error = str(e)
                    await asyncio.sleep(RETRY_SECONDS)
                    continue
                for raw in updates:
                    offset = raw["update_id"] + 1
                    update = parse_update(raw)
                    if update is None:
                        continue
                    for handler in self.handlers:
                        try:
                            await handler(update)
                        except Exception:
                            logger.exception("Ошибка обработки обновления Telegram")

    @asynccontextmanager
    async def running(self):
        task = asyncio.create_task(self._poll())
        try:
            yield self
        finally:
            task.cancel()
