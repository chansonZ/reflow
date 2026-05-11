# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""
Summary generator - generates summaries from conversation history.

This processor now focuses on normal summary generation only:
1. Generates a summary with the LLM when possible.
2. Avoids snippet-concatenation fallback in web scenarios.
3. Leaves final display fallback to FallbackFinalAnswerGenerator.
"""

from __future__ import annotations

import logging

from miroflow.agents.context import AgentContext
from miroflow.io_processor.base import BaseIOProcessor
from miroflow.llm.base import ContextLimitError
from miroflow.registry import ComponentType, register

logger = logging.getLogger(__name__)


@register(ComponentType.IO_PROCESSOR, "SummaryGenerator")
class SummaryGenerator(BaseIOProcessor):
    """Summary generator focused on normal summary generation."""

    USE_PROPAGATE_MODULE_CONFIGS = ("llm", "prompt")

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
            logger.info(
                "Research reached limit (reached_limit=%s, is_final_retry=%s). Skipping summary generation and delegating final fallback to downstream processors.",
                reached_limit,
                is_final_retry,
            )
            return AgentContext(summary_prompt=prompt, summary="")

        try:
            llm_response = await self.llm_client.create_message(
                message_history=message_history
                + [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
            )
        except ContextLimitError:
            logger.warning(
                "Context limit exceeded during summary generation; leaving summary empty for downstream fallback processors"
            )
            return AgentContext(summary_prompt=prompt, summary="")

        return AgentContext(summary_prompt=prompt, summary=llm_response.response_text)
