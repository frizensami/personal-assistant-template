from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from personal_assistant.config import Settings
from personal_assistant.models import CalendarEventSummary, FreeSlot
from personal_assistant.time_utils import now_utc


ASSISTANT_OWNER = "personal-assistant"


class GoogleCalendarClient:
    def __init__(self, settings: Settings, *, timeout: float = 15.0) -> None:
        self.settings = settings
        self.timeout = timeout
        self._access_token: str | None = None
        self._access_token_expiry: datetime | None = None

    async def _get_access_token(self) -> str:
        if (
            self._access_token
            and self._access_token_expiry is not None
            and self._access_token_expiry > now_utc() + timedelta(minutes=1)
        ):
            return self._access_token

        if not (
            self.settings.google_client_id
            and self.settings.google_client_secret
            and self.settings.google_refresh_token
        ):
            raise RuntimeError("Google Calendar credentials are not configured.")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.settings.google_token_url,
                data={
                    "client_id": self.settings.google_client_id,
                    "client_secret": self.settings.google_client_secret,
                    "refresh_token": self.settings.google_refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            response.raise_for_status()
            payload = response.json()
        expires_in = int(payload.get("expires_in", 3600))
        self._access_token = payload["access_token"]
        self._access_token_expiry = now_utc() + timedelta(seconds=expires_in)
        return self._access_token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self._get_access_token()
        url = f"https://www.googleapis.com/calendar/v3/calendars/{self.settings.calendar_id}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            return response.json()

    def _parse_event(self, item: dict[str, Any]) -> CalendarEventSummary:
        start = self._parse_google_datetime(item.get("start", {}))
        end = self._parse_google_datetime(item.get("end", {}))
        private_props = item.get("extendedProperties", {}).get("private", {})
        return CalendarEventSummary(
            id=item["id"],
            title=item.get("summary", "(untitled event)"),
            start=start,
            end=end,
            description=item.get("description", ""),
            owned_by_assistant=private_props.get("assistant_owner") == ASSISTANT_OWNER,
        )

    def _parse_google_datetime(self, payload: dict[str, Any]) -> datetime:
        if "dateTime" in payload:
            return datetime.fromisoformat(payload["dateTime"].replace("Z", "+00:00"))
        date_value = datetime.fromisoformat(payload["date"])
        return date_value.replace(tzinfo=ZoneInfo(self.settings.default_timezone))

    async def list_upcoming_events(
        self,
        *,
        limit: int = 5,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
    ) -> list[CalendarEventSummary]:
        params: dict[str, Any] = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(limit),
            "timeMin": (time_min or now_utc()).isoformat(),
        }
        if time_max is not None:
            params["timeMax"] = time_max.isoformat()
        payload = await self._request("GET", "/events", params=params)
        return [self._parse_event(item) for item in payload.get("items", [])]

    async def create_event(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        description: str = "",
        metadata: dict[str, str] | None = None,
    ) -> CalendarEventSummary:
        private_props = {"assistant_owner": ASSISTANT_OWNER}
        if metadata:
            private_props.update(metadata)
        payload = await self._request(
            "POST",
            "/events",
            json_body={
                "summary": title,
                "description": description,
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": end.isoformat()},
                "extendedProperties": {"private": private_props},
            },
        )
        return self._parse_event(payload)

    async def get_event(self, event_id: str) -> CalendarEventSummary:
        payload = await self._request("GET", f"/events/{event_id}")
        return self._parse_event(payload)

    async def find_owned_event(self, identifier: str) -> CalendarEventSummary | None:
        wanted = identifier.strip().lower()
        if not wanted:
            return None
        try:
            event = await self.get_event(identifier)
            if event.owned_by_assistant:
                return event
        except httpx.HTTPError:
            pass

        events = await self.list_upcoming_events(
            limit=50,
            time_min=now_utc() - timedelta(days=7),
            time_max=now_utc() + timedelta(days=90),
        )
        for event in events:
            if event.owned_by_assistant and (
                event.id == wanted
                or event.id.startswith(wanted)
                or wanted in event.title.lower()
            ):
                return event
        return None

    async def update_owned_event(
        self,
        identifier: str,
        *,
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        description: str | None = None,
    ) -> CalendarEventSummary | None:
        event = await self.find_owned_event(identifier)
        if event is None:
            return None
        payload = await self._request(
            "PATCH",
            f"/events/{event.id}",
            json_body={
                key: value
                for key, value in {
                    "summary": title,
                    "description": description,
                    "start": {"dateTime": start.isoformat()} if start else None,
                    "end": {"dateTime": end.isoformat()} if end else None,
                }.items()
                if value is not None
            },
        )
        return self._parse_event(payload)

    async def delete_owned_event(self, identifier: str) -> bool:
        event = await self.find_owned_event(identifier)
        if event is None:
            return False
        token = await self._get_access_token()
        url = f"https://www.googleapis.com/calendar/v3/calendars/{self.settings.calendar_id}/events/{event.id}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.delete(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
        return True

    async def find_free_slots(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        duration_minutes: int,
    ) -> list[FreeSlot]:
        token = await self._get_access_token()
        url = "https://www.googleapis.com/calendar/v3/freeBusy"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "timeMin": window_start.isoformat(),
                    "timeMax": window_end.isoformat(),
                    "items": [{"id": self.settings.calendar_id}],
                },
            )
            response.raise_for_status()
            payload = response.json()
        busy_ranges = payload.get("calendars", {}).get(self.settings.calendar_id, {}).get("busy", [])
        slots: list[FreeSlot] = []
        cursor = window_start
        for busy in busy_ranges:
            busy_start = self._parse_google_datetime({"dateTime": busy["start"]})
            busy_end = self._parse_google_datetime({"dateTime": busy["end"]})
            if busy_start - cursor >= timedelta(minutes=duration_minutes):
                slots.append(FreeSlot(start=cursor, end=busy_start))
            if busy_end > cursor:
                cursor = busy_end
        if window_end - cursor >= timedelta(minutes=duration_minutes):
            slots.append(FreeSlot(start=cursor, end=window_end))
        return slots

    async def validate(self) -> bool:
        await self.list_upcoming_events(limit=1)
        return True
