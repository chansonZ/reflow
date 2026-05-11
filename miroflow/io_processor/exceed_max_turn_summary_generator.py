# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""
Exceed Max Turn Summary Generator.

Generates retry-oriented summaries only when the agent actually failed due to
turn/context limits and no valid boxed answer exists.
"""

import re

from miroflow.agents.context import AgentContext
from miroflow.benchmark.eval_utils import is_valid_box
from miroflow.io_processor.base import BaseIOProcessor
from miroflow.registry import ComponentType, register
from miroflow.llm.base import ContextLimitError

from miroflow.logging.task_tracer import get_tracer

logger = get_tracer()
# Assistant prefix for failure summary generation (aligned with MiroThinker)
# This guides the model to think first and then output structured content
# fmt: off
FAILURE_SUMMARY_THINK_CONTENT = """We need to write a structured post-mortem style summary **without calling any tools**, explaining why the task was not completed, using these required sections:

* **Failure type**: pick one from **incomplete / blocked / misdirected / format_missed**
* **What happened**: describe the approach taken and why it didn't reach a final answer
* **Useful findings**: list any facts, intermediate results, or conclusions that can be reused"""
# fmt: on

FAILURE_SUMMARY_ASSISTANT_PREFIX = (
    f"<think>\n{FAILURE_SUMMARY_THINK_CONTENT}\n</think>\n\n"
)


@register(ComponentType.IO_PROCESSOR, "ExceedMaxTurnSummaryGenerator")
class ExceedMaxTurnSummaryGenerator(BaseIOProcessor):
    """Generates retry summaries when the task exceeded max turns/context limits."""

    USE_PROPAGATE_MODULE_CONFIGS = ("llm", "prompt")

    @staticmethod
    def _should_generate_exceed_summary(ctx: AgentContext) -> bool:
        """Generate only for retry-relevant failure states without a valid final answer."""
        final_boxed_answer = ctx.get("final_boxed_answer", "")
        if is_valid_box(final_boxed_answer):
            return False

        return bool(ctx.get("reached_limit", False))

    @staticmethod
    def _extract_failure_experience_summary(text: str) -> str:
        """Extract failure experience summary from LLM response text."""
        if not text:
            return ""

        think_matches = list(re.finditer(r"<think>([\s\S]*?)</think>", text))
        last_think_content = ""
        if think_matches:
            last_think_content = think_matches[-1].group(1).strip()

        content = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
        content = re.sub(r"<use_mcp_tool>[\s\S]*", "", content).strip()
        content = re.sub(r"\\boxed\{\s*\}", "", content).strip()

        return content if content else last_think_content

    @staticmethod
    def _fallback_exceed_summary(ctx: AgentContext) -> str:
        """Build a lightweight retry summary without another LLM call."""
        summary = (ctx.get("summary", "") or "").strip()
        if summary:
            return (
                "Failure type: incomplete\n"
                "What happened: The agent hit a turn or context limit before producing a valid final answer.\n"
                f"Useful findings: {summary[:800]}"
            )

        return (
            "Failure type: incomplete\n"
            "What happened: The agent hit a turn or context limit before producing a valid final answer.\n"
            "Useful findings: No reusable findings were extracted before the run stopped."
        )

    async def run_internal(self, ctx: AgentContext) -> AgentContext:
        if not self._should_generate_exceed_summary(ctx):
            return AgentContext(exceed_max_turn_summary=None)

        prompt = self.prompt_manager.render_prompt(
            "exceed_max_turn_summary_prompt", context={}
        )

        message_history = ctx.get("message_history", []).copy()
        if message_history and message_history[-1].get("role") == "user":
            message_history.pop()

        message_history.append(
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        )
        message_history.append(
            {"role": "assistant", "content": FAILURE_SUMMARY_ASSISTANT_PREFIX}
        )

        try:
            llm_response = await self.llm_client.create_message(
                message_history=message_history
            )
            logger.debug("ExceedMaxTurnSummaryGenerator llm_response received")
        except ContextLimitError:
            logger.warning(
                "Context limit exceeded while generating exceed_max_turn_summary; using fallback summary"
            )
            return AgentContext(
                exceed_max_turn_summary=self._fallback_exceed_summary(ctx)
            )

        if llm_response.response_text:
            full_text = FAILURE_SUMMARY_ASSISTANT_PREFIX + llm_response.response_text
            summary = self._extract_failure_experience_summary(full_text)
            return AgentContext(exceed_max_turn_summary=summary)

        return AgentContext(
            exceed_max_turn_summary=self._fallback_exceed_summary(ctx)
        )
