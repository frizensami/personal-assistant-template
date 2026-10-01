from __future__ import annotations

import json
import time
from datetime import timedelta
from textwrap import dedent
from typing import Any, Awaitable, Callable, get_args

import httpx

from personal_assistant.config import Settings
from personal_assistant.models import AssistantAction, AssistantPlan
from personal_assistant.time_utils import now_utc


class OpenAIPlanner:
    def __init__(self, settings: Settings, *, timeout: float = 20.0) -> None:
        self.settings = settings
        self.timeout = timeout

    async def plan(
        self,
        *,
        user_message: str,
        summary: str,
        context_snippets: list[str],
    ) -> AssistantPlan:
        plan, _ = await self.plan_with_trace(
            user_message=user_message,
            summary=summary,
            context_snippets=context_snippets,
        )
        return plan

    async def plan_with_trace(
        self,
        *,
        user_message: str,
        summary: str,
        context_snippets: list[str],
    ) -> tuple[AssistantPlan, dict[str, Any]]:
        if not self.settings.openai_api_key:
            plan = AssistantPlan(
                action="reply",
                response="I need more detail for that, and OpenAI is not configured yet.",
            )
            return plan, {
                "model": self.settings.openai_model,
                "base_url": self.settings.openai_base_url,
                "user_message": user_message,
                "summary": summary,
                "context_snippets": list(context_snippets),
                "parsed_plan": plan.model_dump(mode="json"),
                "error": "OPENAI_API_KEY is not configured.",
            }
        allowed_actions = "\n".join(f"            - {action}" for action in get_args(AssistantAction))

        system_prompt = dedent(
            f"""
            You are the reasoning layer for a single-user personal assistant.

            Return JSON only with this schema:
            {{
              "action": "<one allowed action>",
              "args": {{ ... }},
              "response": "optional short reply"
            }}

            Allowed actions:
{allowed_actions}

            Rules:
            - Keep args compact and deterministic.
            - Use ISO 8601 timestamps when providing datetimes.
            - If the user wants direct information or clarification only, use "reply".
            - If the user asks for multiple safe things in one request, include them in one coherent answer or use the agent tool loop rather than dropping steps.
            - Current timezone: {self.settings.default_timezone}.
            """
        ).strip()

        user_prompt = dedent(
            f"""
            Conversation summary:
            {summary or "(empty)"}

            Context snippets:
            {chr(10).join(f"- {item}" for item in context_snippets) or "- (none)"}

            User message:
            {user_message}
            """
        ).strip()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        payload, content, metadata = await self._chat_json(messages=messages)
        plan = AssistantPlan.model_validate(payload)
        trace = {
            "mode": "planner",
            "model": self.settings.openai_model,
            "base_url": self.settings.openai_base_url,
            "user_message": user_message,
            "summary": summary,
            "context_snippets": list(context_snippets),
            "request_payload": metadata["request_payload"],
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_content": content,
            "parsed_plan": plan.model_dump(mode="json"),
            "usage": metadata["usage"],
            "response_headers": metadata["response_headers"],
            "rate_limits": metadata["rate_limits"],
            "request_id": metadata["request_id"],
            "duration_ms": metadata["duration_ms"],
            "error": "",
        }
        return plan, trace

    async def run_agent(
        self,
        *,
        user_message: str,
        summary: str,
        context_snippets: list[str],
        tools: list[dict[str, Any]],
        tool_runner: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        max_steps: int = 12,
    ) -> tuple[str, dict[str, Any]]:
        if not self.settings.openai_api_key:
            return "AI routing is not configured yet.", {
                "mode": "agent",
                "model": self.settings.openai_model,
                "base_url": self.settings.openai_base_url,
                "user_message": user_message,
                "summary": summary,
                "context_snippets": list(context_snippets),
                "parsed_plan": {},
                "final_response": "AI routing is not configured yet.",
                "error": "OPENAI_API_KEY is not configured.",
                "tool_steps": [],
            }

        tool_descriptions = "\n".join(
            f"- {tool['name']}: {tool['description']}\n  Args: {json.dumps(tool['args'], sort_keys=True)}"
            for tool in tools
        )
        system_prompt = dedent(
            f"""
            You are an autonomous operations agent for a single-user personal assistant.

            You can inspect durable state, query calendar data, execute existing assistant actions,
            and then return a final user-facing answer.

            Available tools:
            {tool_descriptions}

            Rules:
            - Use tools whenever you need information or need to perform a state-changing action.
            - You may use multiple tools, but keep the number of steps tight.
            - If the user asks for multiple concrete actions in one request, execute all of them if they are safe and within scope before you finish.
            - Prefer existing assistant actions via execute_plan when they fit the task.
            - Use execute_plan_batch for multi-step deterministic changes when one request clearly contains several actions.
            - If the request needs state changes that do not fit a single assistant action cleanly, use the direct state-management tools.
            - If the user asks to combine or merge tasks, prefer manage_task with operation="merge" instead of creating a duplicate task.
            - Use the raw state search/read tools when the answer depends on inbox notes, routines, memory, preferences markdown, automation notes, or other state files outside the structured entities.
            - If the change is destructive, broad, or privacy-sensitive, prefer a dry_run tool call first and then finish with a concise confirmation question.
            - If a tool reports ambiguity or missing information, finish with a short follow-up question to the user instead of guessing.
            - If the user's request can be satisfied directly from tool results, finish with a concise answer.
            - If the request suggests a feature that should become deterministic later, include one automation_candidate.
            - Current timezone: {self.settings.default_timezone}.

            Return JSON only with one of these shapes:

            Tool call:
            {{
              "type": "tool_call",
              "tool": "<tool name>",
              "args": {{ ... }},
              "automation_candidate": {{
                "title": "...",
                "why": "...",
                "example_request": "...",
                "suggested_command": "...",
                "suggested_code_path": "..."
              }} | null
            }}

            Final answer:
            {{
              "type": "finish",
              "message": "<user-facing answer>",
              "automation_candidate": {{
                "title": "...",
                "why": "...",
                "example_request": "...",
                "suggested_command": "...",
                "suggested_code_path": "..."
              }} | null
            }}
            """
        ).strip()
        user_prompt = dedent(
            f"""
            Conversation summary:
            {summary or "(empty)"}

            Context snippets:
            {chr(10).join(f"- {item}" for item in context_snippets) or "- (none)"}

            User request:
            {user_message}
            """
        ).strip()
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        tool_names = {str(tool["name"]) for tool in tools}
        tool_steps: list[dict[str, Any]] = []
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        last_candidate: dict[str, Any] = {}
        last_metadata = {
            "request_payload": {"mode": "agent", "tools": tools, "max_steps": max_steps},
            "response_headers": {},
            "rate_limits": {},
            "request_id": "",
            "duration_ms": 0,
        }
        for step in range(1, max_steps + 1):
            decision, content, metadata = await self._chat_json(messages=messages)
            last_metadata = metadata
            usage = metadata["usage"]
            for key in total_usage:
                total_usage[key] += int(usage.get(key) or 0)
            decision_type = str(decision.get("type") or "").strip().lower()
            candidate = decision.get("automation_candidate")
            if isinstance(candidate, dict) and candidate:
                last_candidate = candidate
            if decision_type == "finish":
                message = str(decision.get("message") or "").strip() or "Done."
                return message, {
                    "mode": "agent",
                    "model": self.settings.openai_model,
                    "base_url": self.settings.openai_base_url,
                    "user_message": user_message,
                    "summary": summary,
                    "context_snippets": list(context_snippets),
                    "request_payload": {
                        "mode": "agent",
                        "tools": tools,
                        "max_steps": max_steps,
                    },
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                    "response_content": content,
                    "parsed_plan": {"action": "agent"},
                    "usage": total_usage,
                    "response_headers": metadata["response_headers"],
                    "rate_limits": metadata["rate_limits"],
                    "request_id": metadata["request_id"],
                    "duration_ms": sum(int(item.get("llm_duration_ms") or 0) for item in tool_steps) + int(metadata["duration_ms"] or 0),
                    "error": "",
                    "tool_steps": tool_steps,
                    "final_response": message,
                    "automation_candidate": last_candidate,
                }

            if decision_type != "tool_call":
                error_text = f"Unexpected agent response type: {decision_type or '(missing)'}"
                return error_text, {
                    "mode": "agent",
                    "model": self.settings.openai_model,
                    "base_url": self.settings.openai_base_url,
                    "user_message": user_message,
                    "summary": summary,
                    "context_snippets": list(context_snippets),
                    "request_payload": {
                        "mode": "agent",
                        "tools": tools,
                        "max_steps": max_steps,
                    },
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                    "response_content": content,
                    "parsed_plan": {"action": "agent"},
                    "usage": total_usage,
                    "response_headers": metadata["response_headers"],
                    "rate_limits": metadata["rate_limits"],
                    "request_id": metadata["request_id"],
                    "duration_ms": metadata["duration_ms"],
                    "error": error_text,
                    "tool_steps": tool_steps,
                    "automation_candidate": last_candidate,
                }

            tool_name = str(decision.get("tool") or "").strip()
            raw_args = decision.get("args")
            tool_args = raw_args if isinstance(raw_args, dict) else {}
            if tool_name not in tool_names:
                tool_result = {"ok": False, "error": f"Unknown tool: {tool_name}"}
            else:
                tool_result = await tool_runner(tool_name, tool_args)
            tool_steps.append(
                {
                    "step": step,
                    "tool": tool_name,
                    "args": tool_args,
                    "result": tool_result,
                    "llm_output": decision,
                    "llm_duration_ms": metadata["duration_ms"],
                }
            )
            messages.extend(
                [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            f"Tool result for {tool_name}:\n"
                            f"{json.dumps(tool_result, ensure_ascii=True, sort_keys=True)}\n"
                            "Continue. If you have enough information, finish."
                        ),
                    },
                ]
            )

        step_limit_message = "I couldn't complete that within the current AI step budget."
        return step_limit_message, {
            "mode": "agent",
            "model": self.settings.openai_model,
            "base_url": self.settings.openai_base_url,
            "user_message": user_message,
            "summary": summary,
            "context_snippets": list(context_snippets),
            "request_payload": {
                "mode": "agent",
                "tools": tools,
                "max_steps": max_steps,
            },
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_content": "",
            "parsed_plan": {"action": "agent"},
            "usage": total_usage,
            "response_headers": last_metadata["response_headers"],
            "rate_limits": last_metadata["rate_limits"],
            "request_id": last_metadata["request_id"],
            "duration_ms": sum(int(item.get("llm_duration_ms") or 0) for item in tool_steps),
            "error": "Agent step limit reached.",
            "tool_steps": tool_steps,
            "final_response": step_limit_message,
            "automation_candidate": last_candidate,
        }

    def describe_config(self) -> dict[str, Any]:
        return {
            "configured": bool(self.settings.openai_api_key),
            "admin_configured": bool(self.settings.openai_admin_api_key),
            "model": self.settings.openai_model,
            "base_url": self.settings.openai_base_url,
        }

    async def get_usage_summary(self, *, days: int = 7) -> dict[str, Any]:
        if not self.settings.openai_admin_api_key:
            return {
                "available": False,
                "reason": "OPENAI_ADMIN_API_KEY is not configured.",
            }
        payload = await self._admin_get(
            "/organization/usage/completions",
            {
                "start_time": int((now_utc() - timedelta(days=max(days, 1))).timestamp()),
                "bucket_width": "1d",
                "limit": max(days, 1),
            },
        )
        totals: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "input_cached_tokens": 0,
            "num_model_requests": 0,
        }
        bucket_count = 0
        for bucket in payload.get("data") or []:
            if not isinstance(bucket, dict):
                continue
            bucket_count += 1
            for result in bucket.get("results") or []:
                if not isinstance(result, dict):
                    continue
                for key in totals:
                    value = result.get(key)
                    if isinstance(value, int):
                        totals[key] += value
        return {
            "available": True,
            "days": max(days, 1),
            "bucket_count": bucket_count,
            **totals,
        }

    async def get_cost_summary(self, *, days: int = 30) -> dict[str, Any]:
        if not self.settings.openai_admin_api_key:
            return {
                "available": False,
                "reason": "OPENAI_ADMIN_API_KEY is not configured.",
            }
        payload = await self._admin_get(
            "/organization/costs",
            {
                "start_time": int((now_utc() - timedelta(days=max(days, 1))).timestamp()),
                "bucket_width": "1d",
                "limit": max(days, 1),
            },
        )
        total_cost = 0.0
        currency = "usd"
        line_items = 0
        for bucket in payload.get("data") or []:
            if not isinstance(bucket, dict):
                continue
            for result in bucket.get("results") or []:
                if not isinstance(result, dict):
                    continue
                amount = result.get("amount") or {}
                if isinstance(amount, dict):
                    value = amount.get("value")
                    if isinstance(value, (int, float)):
                        total_cost += float(value)
                    if isinstance(amount.get("currency"), str) and amount["currency"]:
                        currency = str(amount["currency"])
                line_items += 1
        return {
            "available": True,
            "days": max(days, 1),
            "total_cost": round(total_cost, 6),
            "currency": currency,
            "line_items": line_items,
        }

    async def _admin_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.settings.openai_base_url.rstrip('/')}{path}",
                headers={
                    "Authorization": f"Bearer {self.settings.openai_admin_api_key}",
                    "Content-Type": "application/json",
                },
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
        if isinstance(payload, dict):
            return payload
        raise RuntimeError("OpenAI admin API returned an unexpected response.")

    async def _chat_json(self, *, messages: list[dict[str, str]]) -> tuple[dict[str, Any], str, dict[str, Any]]:
        request_payload = {
            "model": self.settings.openai_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.settings.openai_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json=request_payload,
            )
            response.raise_for_status()
            raw_payload = response.json()
        duration_ms = int((time.perf_counter() - started) * 1000)
        content = raw_payload["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise RuntimeError("OpenAI returned a non-object JSON payload.")
        return parsed, content, {
            "request_payload": request_payload,
            "usage": raw_payload.get("usage") or {},
            "response_headers": self._extract_headers(response.headers),
            "rate_limits": self._extract_rate_limits(response.headers),
            "request_id": str(
                response.headers.get("x-request-id")
                or response.headers.get("openai-request-id")
                or ""
            ).strip(),
            "duration_ms": duration_ms,
        }

    @staticmethod
    def _extract_headers(headers: httpx.Headers) -> dict[str, str]:
        kept: dict[str, str] = {}
        for key in (
            "x-request-id",
            "openai-request-id",
            "x-ratelimit-limit-requests",
            "x-ratelimit-remaining-requests",
            "x-ratelimit-reset-requests",
            "x-ratelimit-limit-tokens",
            "x-ratelimit-remaining-tokens",
            "x-ratelimit-reset-tokens",
        ):
            value = headers.get(key)
            if value:
                kept[key] = value
        return kept

    @staticmethod
    def _extract_rate_limits(headers: httpx.Headers) -> dict[str, str]:
        rate_limits: dict[str, str] = {}
        for source, target in (
            ("x-ratelimit-limit-requests", "limit_requests"),
            ("x-ratelimit-remaining-requests", "remaining_requests"),
            ("x-ratelimit-reset-requests", "reset_requests"),
            ("x-ratelimit-limit-tokens", "limit_tokens"),
            ("x-ratelimit-remaining-tokens", "remaining_tokens"),
            ("x-ratelimit-reset-tokens", "reset_tokens"),
        ):
            value = headers.get(source)
            if value:
                rate_limits[target] = value
        return rate_limits
