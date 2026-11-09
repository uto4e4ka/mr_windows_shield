"""Гостевые ссылки на страницу индекса: одна на сервер, без входа в панель, только просмотр.

Код в ссылке случайный (secrets.token_urlsafe); перевыпуск даёт новый код, старая ссылка перестаёт работать.
"""
import secrets

from pydantic import BaseModel

TOKEN_BYTES = 18                # 24 символа в ссылке


class GuestLink(BaseModel):
    guild_id: int
    token: str


class GuestLinks:
    def __init__(self, data):
        self.links = data.collection("guest_links.json", GuestLink, key="guild_id")

    def token(self, guild_id: int) -> str | None:
        link = self.links.get(guild_id)
        return link.token if link else None

    def issue(self, guild_id: int) -> str:
        """Новая ссылка (или перевыпуск: прежняя перестаёт работать)."""
        return self.links.put(GuestLink(guild_id=guild_id, token=secrets.token_urlsafe(TOKEN_BYTES))).token

    def revoke(self, guild_id: int) -> None:
        self.links.delete(guild_id)

    def guild_for(self, token: str | None) -> int | None:
        if not token:
            return None
        return next((link.guild_id for link in self.links.all() if secrets.compare_digest(link.token, token)), None)
