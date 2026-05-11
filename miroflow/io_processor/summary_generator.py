# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""
Summary generator - generates summaries from conversation history.

This processor now:
1. Generates a normal summary with the LLM when possible.
2. Produces a meaningful fallback summary when the research flow hit a turn limit.
3. Reuses extracted findings when summary generation itself hits a context limit.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from miroflow.agents.context import AgentContext
from miroflow.io_processor.base import BaseIOProcessor
from miroflow.llm.base import ContextLimitError
from miroflow.registry import ComponentType, register

logger = logging.getLogger(__name__)


@register(ComponentType.IO_PROCESSOR, "SummaryGenerator")
class SummaryGenerator(BaseIOProcessor):
    """Summary generator with improved fallback handling."""

    USE_PROPAGATE_MODULE_CONFIGS = ("llm", "prompt")

    @staticmethod
    def _iter_text_payloads(content) -> Iterable[str]:
        """Yield text payloads from mixed message content structures."""
        if isinstance(content, str):
            yield content
            return

        if not isinstance(content, list):
            return

        for item in content:
            if not isinstance(item, dict):
                continue

            item_type = item.get("type")
            if item_type == "text":
                text = item.get("text", "")
                if isinstance(text, str) and text.strip():
                    yield text
            elif item_type == "tool_result":
                payload = item.get("content", "")
                if isinstance(payload, str) and payload.strip():
                    yield payload
                elif isinstance(payload, list):
                    for nested in payload:
                        if isinstance(nested, dict) and nested.get("type") == "text":
                            text = nested.get("text", "")
                            if isinstance(text, str) and text.strip():
                                yield text

    @staticmethod
    def _extract_findings_from_payload(payload: str) -> list[dict[str, str]]:
        """Parse JSON payload and extract title/snippet findings when present."""
        if not payload:
            return []

        try:
            result_data = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return []

        organic_results = result_data.get("organic")
        if not isinstance(organic_results, list):
            return []

        findings: list[dict[str, str]] = []
        for result in organic_results:
            if not isinstance(result, dict):
                continue

            title = str(result.get("title", "") or "").strip()
            snippet = str(result.get("snippet", "") or "").strip()
            if title or snippet:
                findings.append({"title": title, "snippet": snippet})

        return findings

    @staticmethod
    def extract_snippets_from_message_history(message_history: list) -> list[dict[str, str]]:
        """Extract snippet-like findings from all message payloads in history."""
        findings: list[dict[str, str]] = []

        for msg in message_history:
            for payload in SummaryGenerator._iter_text_payloads(msg.get("content", "")):
                findings.extend(SummaryGenerator._extract_findings_from_payload(payload))

        return findings

    @staticmethod
    def _deduplicate_findings(findings: list[dict[str, str]]) -> list[dict[str, str]]:
        seen: set[tuple[str, str]] = set()
        deduped: list[dict[str, str]] = []

        for finding in findings:
            title = str(finding.get("title", "") or "").strip()
            snippet = str(finding.get("snippet", "") or "").strip()
            key = (title, snippet)
            if key in seen:
                continue
            seen.add(key)
            deduped.append({"title": title, "snippet": snippet})

        return deduped

    @staticmethod
    def _extract_key_findings(message_history: list) -> str:
        """
        从消息历史中提取关键发现。
        用于当达到限制或总结阶段超长时提供有意义的摘要。
        """
        findings = SummaryGenerator._deduplicate_findings(
            SummaryGenerator.extract_snippets_from_message_history(message_history)
        )

        if not findings:
            return "基于当前搜索结果的初步总结。"

        summary_lines = ["**研究发现汇总：**", ""]
        for i, finding in enumerate(findings[:8], 1):
            title = finding.get("title", "")
            snippet = finding.get("snippet", "")
            if title:
                summary_lines.append(f"{i}. **{title}**")
            else:
                summary_lines.append(f"{i}.")
            if snippet:
                summary_lines.append(f"   摘要：{snippet[:200]}...")

        return "\n".join(summary_lines)

    async def run_internal(self, ctx: AgentContext) -> AgentContext:
        prompt = self.prompt_manager.render_prompt(
            "summarize_prompt",
            context=dict(
                task_description=ctx.get("task_description"),
                task_failed=ctx.get("task_failed", False),
            ),
        )

        reached_limit = ctx.get("reached_limit", False)
        is_final_retry = ctx.get("is_final_retry", False)
        message_history = ctx.get("message_history", [])

        if reached_limit and not is_final_retry:
            fallback_summary = SummaryGenerator._extract_key_findings(message_history)
            logger.info(
                "Research reached limit (reached_limit=%s, is_final_retry=%s). Using extracted findings as summary.",
                reached_limit,
                is_final_retry,
            )
            return AgentContext(
                summary_prompt=prompt,
                summary=fallback_summary,
                exceed_max_turn_summary=(
                    "⚠️ 研究因系统限制而停止。以上是基于已收集信息的总结。"
                ),
            )

        try:
            llm_response = await self.llm_client.create_message(
                message_history=message_history
                + [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
            )
        except ContextLimitError:
            logger.warning("Context limit exceeded during summary generation; using extracted findings fallback")
            fallback_summary = SummaryGenerator._extract_key_findings(message_history)
            return AgentContext(
                summary_prompt=prompt,
                summary=fallback_summary,
                exceed_max_turn_summary="⚠️ 由于上下文长度限制，总结生成中止。",
            )

        return AgentContext(summary_prompt=prompt, summary=llm_response.response_text)
