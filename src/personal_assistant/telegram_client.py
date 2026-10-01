from __future__ import annotations

import httpx


class TelegramClient:
    def __init__(self, bot_token: str, *, timeout: float = 10.0) -> None:
        self.bot_token = bot_token
        self.timeout = timeout

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
    ) -> int | None:
        if not self.bot_token:
            return None
        payload: dict = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        payload["disable_web_page_preview"] = disable_web_page_preview
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            message = body.get("result") or {}
            message_id = message.get("message_id")
            return int(message_id) if message_id is not None else None

    async def delete_message(self, chat_id: str | int, message_id: int) -> None:
        if not self.bot_token:
            return
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.bot_token}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
            )
            response.raise_for_status()

    async def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        if not self.bot_token:
            return
        payload: dict = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery",
                json=payload,
            )
            response.raise_for_status()

    async def edit_message_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
    ) -> None:
        if not self.bot_token:
            return
        payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        payload["disable_web_page_preview"] = disable_web_page_preview
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.bot_token}/editMessageText",
                json=payload,
            )
            response.raise_for_status()

    async def edit_message_reply_markup(
        self,
        chat_id: str | int,
        message_id: int,
        reply_markup: dict | None,
    ) -> None:
        if not self.bot_token:
            return
        payload: dict = {"chat_id": chat_id, "message_id": message_id}
        payload["reply_markup"] = reply_markup or {}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.bot_token}/editMessageReplyMarkup",
                json=payload,
            )
            response.raise_for_status()

    async def set_webhook(self, webhook_url: str) -> dict:
        if not self.bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.bot_token}/setWebhook",
                json={"url": webhook_url},
            )
            response.raise_for_status()
            return response.json()
