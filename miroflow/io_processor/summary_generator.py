# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""
Summary generator - generates summaries from conversation history

改进版本：处理 reached_limit 情况下的有意义总结
"""

from miroflow.io_processor.base import BaseIOProcessor
from miroflow.agents.context import AgentContext
from miroflow.registry import register, ComponentType
from miroflow.llm.base import ContextLimitError
import json
import logging

logger = logging.getLogger(__name__)


@register(ComponentType.IO_PROCESSOR, "SummaryGenerator")
class SummaryGenerator(BaseIOProcessor):
    """Summary generator with improved fallback handling"""

    USE_PROPAGATE_MODULE_CONFIGS = ("llm", "prompt")
    
    @staticmethod
    def extract_snippets_from_message_history(message_history):
        findings = []
        
        for msg in message_history:
            # print(f'in _extract_key_findings: 【{msg}】\n')
            
            # 查找用户消息中的内容
            if msg.get("role") == "user":
                content = msg.get("content", "")
                
                if isinstance(content, list):
                    # 遍历content列表中的每一个元素
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            try:
                                # 解析 text 内容为 JSON
                                result_data = json.loads(item.get("text", "{}"))
                                
                                # 检查并提取 organic 字段
                                if "organic" in result_data:
                                    # 循环处理 organic 字段中的每一个结果
                                    for result in result_data["organic"]:
                                        title = result.get("title", "")
                                        snippet = result.get("snippet", "")
                                        if title or snippet:
                                            findings.append({
                                                "title": title,
                                                "snippet": snippet
                                            })
                            except json.JSONDecodeError:
                                # 不是 JSON，跳过
                                print(f"JSON 解析失败: {item.get('text', '')}")
                                pass
        
        return findings

    @staticmethod
    def _extract_key_findings(message_history: list) -> str:
        """
        从消息历史中提取关键发现
        用于当达到限制时提供有意义的摘要
        """
        findings = []
        
        for msg in message_history:
            print(f'in _extract_key_findings: 【{msg}】\n')
            # 查找用户消息中的工具结果
            if msg.get("role") == "user":
                content = msg.get("content", "")
                
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_result":
                            try:
                                result_data = json.loads(item.get("content", "{}"))
                                
                                # 提取搜索结果
                                if result_data.get("organic"):
                                    for result in result_data["organic"][:3]:
                                        title = result.get("title", "")
                                        snippet = result.get("snippet", "")
                                        if title or snippet:
                                            findings.append({
                                                "title": title,
                                                "snippet": snippet
                                            })
                            except json.JSONDecodeError:
                                # 不是 JSON，跳过
                                pass
        
        # 构建摘要
        if not findings:
            #z 再简单总结下
            findings = SummaryGenerator.extract_snippets_from_message_history(message_history)
            if not findings:
                return "基于当前搜索结果的初步总结。"
        #z :应该再让大模型总结下，而不是直接输出
        summary = "**研究发现汇总：**\n\n"
        for i, finding in enumerate(findings, 1):
            if finding["title"]:
                summary += f"{i}. **{finding['title']}**\n"
            if finding["snippet"]:
                snippet = finding["snippet"][:120]
                summary += f"   摘要：{snippet}...\n"
        
        return summary

    async def run_internal(self, ctx: AgentContext) -> AgentContext:
        prompt = self.prompt_manager.render_prompt(
            "summarize_prompt",
            context=dict(
                task_description=ctx.get("task_description"),
                task_failed=ctx.get("task_failed", False),
            ),
        )

        # ===== 改进：当达到限制时，提供有意义的摘要 =====
        reached_limit = ctx.get("reached_limit", False)
        is_final_retry = ctx.get("is_final_retry", False)
        
        if reached_limit and not is_final_retry:
            print(f'#z: reached_limit = {reached_limit}, is_final_retry = {is_final_retry}')
            # 之前的代码：返回无意义的占位符
            return AgentContext(
                summary_prompt=prompt,
                summary="Task incomplete - skipping answer generation to retry with failure experience.",
            )
            
            # 改进版本：尝试从现有信息中提取摘要
            # message_history = ctx.get("message_history", [])
            # fallback_summary = SummaryGenerator._extract_key_findings(message_history)
            # #z INFO:miroflow.io_processor.summary_generator:Research reached limit (reached_limit=True, is_final_retry=False). Using extracted findings as summary.
            # logger.info(
            #     f"Research reached limit (reached_limit={reached_limit}, "
            #     f"is_final_retry={is_final_retry}). "
            #     f"Using extracted findings as summary."
            # )
            
            # return AgentContext(
            #     summary_prompt=prompt,
            #     summary=fallback_summary,
            #     exceed_max_turn_summary=(
            #         f"⚠️ 研究因系统限制而停止。"
            #         f"以上是基于已收集信息的总结。"
            #     ),
            # )
        # ===== 改进结束 =====

        message_history = ctx.get("message_history", [])
        try:
            print(f'总结的系统prompt：{prompt}')
            llm_response = await self.llm_client.create_message(
                message_history=message_history
                + [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
            )
        except ContextLimitError:
            return AgentContext(
                summary_prompt=prompt,
                summary="Task interrupted due to context limit.",
            )
        # except ContextLimitError:#z 上下文超长报错
        #     logger.warning("Context limit exceeded during summary generation")
            
        #     # ===== 也为 ContextLimitError 提供备用摘要 =====
        #     fallback_summary = SummaryGenerator._extract_key_findings(message_history)
        #     return AgentContext(
        #         summary_prompt=prompt,
        #         summary=fallback_summary,
        #         exceed_max_turn_summary="⚠️ 由于上下文长度限制，总结生成中止。",
        #     )
        #     # ===== 备用摘要结束 =====

        # Return both summary_prompt and summary in agent state
        return AgentContext(summary_prompt=prompt, summary=llm_response.response_text)