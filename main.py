"""
SubAgent WorkTogether Plugin / 子代理协作插件

Registers LLM function-calling tools so that any agent can delegate tasks
to other configured sub-agents and receive their responses.
Supports cross-agent delegation with recursion depth protection,
per-agent call count limiting, and delegation trace reporting
(all configurable via WebUI).

注册 LLM function-calling 工具，使任意 Agent 都可以将任务委派给其他
已配置的子代理，并获取其回复结果。
支持跨代理委派，并带有递归深度保护、单代理调用次数限制和委派追踪报告
（均可通过 WebUI 配置）。
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime
import html as _html
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import llm_tool
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.handoff import HandoffTool
from astrbot.core.agent.message import Message
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.register import llm_tools

# Tracks the current delegation depth per async call chain to prevent infinite loops.
_delegation_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_delegation_depth", default=0
)

# Tracks which agent is currently executing so we can block self-delegation.
# None means the top-level caller (user / main agent pipeline).
_current_agent: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_agent", default=None
)

# Per-caller-target pair counts: tracks how many times the currently executing
# agent has delegated to each specific target within its current execution.
# Resets to a fresh dict each time a new agent starts executing, so chains
# like A→B→C→A give A a clean slate upon re-entry.
_caller_target_counts: contextvars.ContextVar[dict[str, int] | None] = (
    contextvars.ContextVar("_caller_target_counts", default=None)
)

# Key used to store per-agent call counts on the event object via set_extra/get_extra.
# Counts are scoped per-event so they accumulate across sibling calls and are
# automatically cleaned up when the event is garbage-collected.
_CALL_COUNTS_KEY = "_subagent_call_counts"
_TOTAL_DELEGATION_COUNT_KEY = "_subagent_total_delegation_count"
_DELEGATION_TRACE_KEY = "_delegation_trace"

MAIN_AGENT_NAME = "main"

_DEFAULT_MAX_DEPTH = 3
_DEFAULT_MAX_CALLS = 3
_DEFAULT_MAX_CALLS_PER_PAIR = 3
_DEFAULT_MAX_TOTAL_DELEGATIONS = 10
_DEFAULT_DELEGATION_TIMEOUT = 120.0
_DEFAULT_MAX_TASK_LENGTH = 4000
_DEFAULT_AUTO_SEND_TRACE = False
_DEFAULT_DISABLE_NATIVE_HANDOFFS = False

_ERROR_PREFIX = "[DELEGATION_ERROR]"


def _parse_bool(value: object, default: bool = False) -> bool:
    """Explicitly parse a boolean from config values that might be str/int/bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return default


def _safe_int(value: Any, default: int, *, min_val: int | None = None) -> int:
    """Parse an int from config with fallback and optional minimum."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if min_val is not None and result < min_val:
        result = min_val
    return result


def _safe_float(
    value: Any, default: float, *, min_val: float | None = None
) -> float:
    """Parse a float from config with fallback and optional minimum."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if min_val is not None and result < min_val:
        result = min_val
    return result

_TRACE_HTML_TEMPLATE = """\
<div style="font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; \
max-width: 640px; padding: 24px; background: #fff;">
  <h2 style="border-bottom: 2px solid #4A90D9; padding-bottom: 8px; \
margin-top: 0; color: #333;">
    Delegation Trace Report
  </h2>
  {% for entry in trace %}
  <div style="margin: 14px 0; padding: 12px; border-radius: 6px; \
border-left: 4px solid {{ '#4A90D9' if entry.status == 'success' else '#E74C3C' }}; \
background: {{ '#F7FAFC' if entry.status == 'success' else '#FDF2F2' }};">
    <div style="font-size: 12px; color: #999; margin-bottom: 4px;">
      Step {{ loop.index }} &middot; Depth {{ entry.depth }} \
&middot; {{ entry.time_str }}
    </div>
    <div style="font-size: 15px; margin: 6px 0;">
      <strong style="color: #4A90D9;">{{ entry.caller }}</strong>
      <span style="color: #999;">&rarr;</span>
      <strong style="color: #2ECC71;">{{ entry.target }}</strong>
    </div>
    <div style="font-size: 13px; color: #555; margin: 6px 0; \
line-height: 1.5;">
      <em>Task:</em> {{ entry.task_short }}
    </div>
    <div style="font-size: 13px; color: #333; margin: 6px 0; \
line-height: 1.5;">
      <em>Response:</em> {{ entry.response_short }}
    </div>
  </div>
  {% endfor %}
  <div style="font-size: 12px; color: #bbb; margin-top: 18px; \
text-align: right;">
    Total steps: {{ trace|length }}
  </div>
</div>
"""


def _get_call_counts(event: AstrMessageEvent) -> dict[str, int]:
    """Retrieve or initialize the per-event agent call counts dict."""
    counts = event.get_extra(_CALL_COUNTS_KEY)
    if counts is None:
        counts = {}
        event.set_extra(_CALL_COUNTS_KEY, counts)
    return counts


def _get_total_delegation_count(event: AstrMessageEvent) -> int:
    """Retrieve the per-event total delegation count."""
    return event.get_extra(_TOTAL_DELEGATION_COUNT_KEY, 0)


def _increment_total_delegation_count(event: AstrMessageEvent) -> int:
    """Increment and return the per-event total delegation count."""
    count = _get_total_delegation_count(event) + 1
    event.set_extra(_TOTAL_DELEGATION_COUNT_KEY, count)
    return count


def _get_caller_target_counts() -> dict[str, int]:
    """Retrieve or initialize the per-caller-target counts dict for the
    currently executing agent.  Each agent execution gets a fresh dict."""
    counts = _caller_target_counts.get()
    if counts is None:
        counts = {}
        _caller_target_counts.set(counts)
    return counts


def _get_delegation_trace(event: AstrMessageEvent) -> list[dict]:
    """Retrieve or initialize the per-event delegation trace list."""
    trace = event.get_extra(_DELEGATION_TRACE_KEY)
    if trace is None:
        trace = []
        event.set_extra(_DELEGATION_TRACE_KEY, trace)
    return trace


@register(
    "subagent_worktogether",
    "auguscao",
    "Provides LLM tools for multi-agent delegation with trace reporting.",
    "1.1.0",
)
class SubAgentWorkTogether(Star):
    context: Context

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        cfg = config or {}
        self.max_delegation_depth: int = _safe_int(
            cfg.get("max_delegation_depth", _DEFAULT_MAX_DEPTH),
            _DEFAULT_MAX_DEPTH,
            min_val=1,
        )
        self.max_calls_per_agent: int = _safe_int(
            cfg.get("max_calls_per_agent", _DEFAULT_MAX_CALLS),
            _DEFAULT_MAX_CALLS,
            min_val=1,
        )
        self.max_calls_per_pair: int = _safe_int(
            cfg.get("max_calls_per_pair", _DEFAULT_MAX_CALLS_PER_PAIR),
            _DEFAULT_MAX_CALLS_PER_PAIR,
            min_val=1,
        )
        self.max_total_delegations: int = _safe_int(
            cfg.get("max_total_delegations", _DEFAULT_MAX_TOTAL_DELEGATIONS),
            _DEFAULT_MAX_TOTAL_DELEGATIONS,
            min_val=1,
        )
        self.delegation_timeout: float = _safe_float(
            cfg.get("delegation_timeout", _DEFAULT_DELEGATION_TIMEOUT),
            _DEFAULT_DELEGATION_TIMEOUT,
            min_val=1.0,
        )
        self.max_task_length: int = _safe_int(
            cfg.get("max_task_length", _DEFAULT_MAX_TASK_LENGTH),
            _DEFAULT_MAX_TASK_LENGTH,
            min_val=0,
        )
        self.auto_send_trace: bool = _parse_bool(
            cfg.get("auto_send_trace", _DEFAULT_AUTO_SEND_TRACE),
            _DEFAULT_AUTO_SEND_TRACE,
        )
        self.disable_native_handoffs: bool = _parse_bool(
            cfg.get("disable_native_handoffs", _DEFAULT_DISABLE_NATIVE_HANDOFFS),
            _DEFAULT_DISABLE_NATIVE_HANDOFFS,
        )
        self._last_traces: dict[str, list[dict]] = {}

    async def initialize(self) -> None:
        self._sync_native_handoff_state()

    async def terminate(self) -> None:
        self._reactivate_native_handoffs()

    @filter.on_llm_request()
    async def _on_llm_request(self, event: AstrMessageEvent, request) -> None:
        """Sanitize the request ToolSet before the LLM sees it.

        1. Remove native HandoffTools when ``disable_native_handoffs`` is True.
           Setting ``active=False`` alone is insufficient because the default
           "full" schema mode (openai_schema / anthropic_schema / google_schema)
           does **not** check the ``active`` flag.
        2. Remove ``send_delegation_summary`` when ``auto_send_trace`` is False
           so the LLM cannot spontaneously call it without being prompted.
        """
        if not hasattr(request, "func_tool") or not request.func_tool:
            return

        tools = getattr(request.func_tool, "tools", None)
        if not isinstance(tools, list):
            return

        names_to_remove: set[str] = set()

        if self.disable_native_handoffs:
            self._sync_native_handoff_state()
            for t in tools:
                if isinstance(t, HandoffTool):
                    names_to_remove.add(t.name)

        if not self.auto_send_trace:
            names_to_remove.add("send_delegation_summary")

        if not names_to_remove:
            return

        before = len(tools)
        request.func_tool.tools = [
            t
            for t in tools
            if not (hasattr(t, "name") and t.name in names_to_remove)
        ]
        removed = before - len(request.func_tool.tools)
        if removed:
            logger.debug(
                "on_llm_request: removed %d tool(s) from request ToolSet: %s",
                removed,
                names_to_remove,
            )

    def _sync_native_handoff_state(self) -> None:
        """Deactivate native HandoffTools when disable_native_handoffs is True."""
        orch = self.context.subagent_orchestrator
        if not orch or not orch.handoffs:
            return
        for h in orch.handoffs:
            if self.disable_native_handoffs:
                h.active = False
            else:
                h.active = True

    def _reactivate_native_handoffs(self) -> None:
        """Re-enable native HandoffTools (called on plugin terminate/reload)."""
        orch = self.context.subagent_orchestrator
        if not orch or not orch.handoffs:
            return
        for h in orch.handoffs:
            h.active = True

    # ------------------------------------------------------------------ #
    # LLM Tool: delegate_task_to_agent
    # ------------------------------------------------------------------ #
    @llm_tool(name="delegate_task_to_agent")
    async def delegate_task_to_agent(
        self,
        event: AstrMessageEvent,
        agent_name: str,
        task: str,
    ) -> str:
        """Delegate a task to a specific agent and return its response. Use this tool when you want another specialized agent to handle a sub-task. You can also delegate to "main" to ask the main agent.

        IMPORTANT: If the result starts with "[DELEGATION_ERROR]", it means the delegation failed due to a system-level issue (not a normal answer). You should NOT treat such results as the agent's answer. Instead, try to solve the task yourself, use a different agent, or inform the user about the failure.

        Args:
            agent_name(string): The name of the target agent. Use "main" for the main agent, or use list_available_agents to see all available agents.
            task(string): A SELF-CONTAINED task description. Include all necessary context — do not use pronouns like 'it' or 'this' that refer to earlier conversation.
        """
        self._sync_native_handoff_state()

        # --- Input validation ---
        if not agent_name or not agent_name.strip():
            return f"{_ERROR_PREFIX} agent_name must not be empty."

        if not task or not task.strip():
            return f"{_ERROR_PREFIX} task must not be empty."

        if self.max_task_length > 0 and len(task) > self.max_task_length:
            return (
                f"{_ERROR_PREFIX} Task description too long ({len(task)} chars, "
                f"limit: {self.max_task_length}). Please shorten the task."
            )

        # --- Recursion depth guard ---
        current_depth = _delegation_depth.get()
        if current_depth >= self.max_delegation_depth:
            return (
                f"{_ERROR_PREFIX} Maximum delegation depth ({self.max_delegation_depth}) reached. "
                f"Cannot delegate further. "
                f"Please answer directly based on available information."
            )

        # --- Global delegation circuit breaker ---
        total = _get_total_delegation_count(event)
        if total >= self.max_total_delegations:
            return (
                f"{_ERROR_PREFIX} Maximum total delegations ({self.max_total_delegations}) "
                f"reached for this conversation. "
                f"Please answer directly based on available information."
            )

        # --- Per-agent call count guard (stored on event, accumulates across calls) ---
        counts = _get_call_counts(event)
        target_key = agent_name.lower()
        current_calls = counts.get(target_key, 0)
        if current_calls >= self.max_calls_per_agent:
            return (
                f"{_ERROR_PREFIX} Agent '{agent_name}' has already been called "
                f"{current_calls} time(s) in this event "
                f"(limit: {self.max_calls_per_agent}). "
                f"Please answer directly or use a different agent."
            )

        # --- Self-delegation guard ---
        caller = _current_agent.get()
        if caller is not None and target_key == caller:
            return (
                f"{_ERROR_PREFIX} Agent '{agent_name}' cannot delegate to itself. "
                f"Please use a different agent or answer directly."
            )

        # --- Per-caller-target pair guard (resets each time an agent re-enters) ---
        ct_counts = _get_caller_target_counts()
        ct_current = ct_counts.get(target_key, 0)
        if ct_current >= self.max_calls_per_pair:
            return (
                f"{_ERROR_PREFIX} You have already delegated to agent '{agent_name}' "
                f"{ct_current} time(s) in your current execution "
                f"(limit: {self.max_calls_per_pair}). "
                f"Please try a different agent or answer directly."
            )

        orch = self.context.subagent_orchestrator

        # --- Validate target existence before consuming quota ---
        caller_name = _current_agent.get() or "(user)"
        is_main = agent_name.lower() == MAIN_AGENT_NAME
        handoff = None
        if not is_main:
            if not orch or not orch.handoffs:
                return f"{_ERROR_PREFIX} No subagent orchestrator configured or no agents available."
            handoff = self._find_handoff(orch.handoffs, agent_name)
            if not handoff:
                available = [h.agent.name for h in orch.handoffs] + [MAIN_AGENT_NAME]
                return (
                    f"{_ERROR_PREFIX} Agent '{agent_name}' not found. "
                    f"Available agents: {', '.join(available)}"
                )

        # Increment counts only after target is confirmed reachable.
        counts[target_key] = current_calls + 1
        ct_counts[target_key] = ct_current + 1
        _increment_total_delegation_count(event)

        # --- Perform delegation ---
        if is_main:
            result = await self._invoke_main_agent(event, task, current_depth)
        else:
            assert handoff is not None  # guaranteed by early-return above
            try:
                llm_resp = await self._invoke_subagent(
                    event, handoff, task, current_depth
                )
                result = (
                    llm_resp.completion_text or "(Agent returned empty response)"
                )
            except Exception as e:
                logger.error(
                    "Failed to delegate task to agent '%s'",
                    agent_name,
                    exc_info=True,
                )
                result = (
                    f"{_ERROR_PREFIX} Delegation to agent '{agent_name}' failed: {e}. "
                    f"Please try to solve the task yourself or use another agent."
                )

        # --- Record trace entry ---
        is_error = result.startswith(_ERROR_PREFIX)
        trace = _get_delegation_trace(event)
        trace.append(
            {
                "caller": caller_name,
                "target": agent_name,
                "task": task,
                "response": result,
                "status": "error" if is_error else "success",
                "depth": current_depth,
                "timestamp": time.time(),
            }
        )
        self._last_traces = {event.unified_msg_origin: list(trace)}

        # --- Auto-send hint (only at top-level, only once per event) ---
        if self.auto_send_trace and current_depth == 0 and not is_error:
            already_hinted = event.get_extra("_trace_hint_sent", False)
            if not already_hinted:
                event.set_extra("_trace_hint_sent", True)
                result += (
                    "\n\n[System] After you have completed ALL delegations for "
                    "the user's request, please call the `send_delegation_summary` "
                    "tool exactly ONCE to send the collaboration trace report "
                    "to the user."
                )

        return result

    # ------------------------------------------------------------------ #
    # LLM Tool: list_available_agents
    # ------------------------------------------------------------------ #
    @llm_tool(name="list_available_agents")
    async def list_available_agents(self, event: AstrMessageEvent) -> str:
        """List all available agents that can be delegated tasks to via delegate_task_to_agent tool."""
        lines = [f"- {MAIN_AGENT_NAME}: The main/general-purpose agent [tools: all]"]

        orch = self.context.subagent_orchestrator
        if orch and orch.handoffs:
            for h in orch.handoffs:
                desc = h.description or "(no description)"
                tools = h.agent.tools
                if tools is None:
                    tools_str = "all"
                elif not tools:
                    tools_str = "none"
                else:
                    tools_str = ", ".join(str(t) for t in tools)
                lines.append(f"- {h.agent.name}: {desc} [tools: {tools_str}]")

        current_depth = _delegation_depth.get()
        counts = _get_call_counts(event)
        total = _get_total_delegation_count(event)
        lines.append(
            f"\nCurrent delegation depth: {current_depth}/{self.max_delegation_depth}"
        )
        lines.append(f"Max calls per agent: {self.max_calls_per_agent}")
        lines.append(f"Total delegations used: {total}/{self.max_total_delegations}")
        caller = _current_agent.get()
        if caller:
            lines.append(f"Current agent: {caller} (cannot delegate to itself)")
        if counts:
            calls_info = ", ".join(f"{k}: {v}" for k, v in counts.items())
            lines.append(f"Agent call counts in this event: {calls_info}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # LLM Tool: send_delegation_summary
    # ------------------------------------------------------------------ #
    @llm_tool(name="send_delegation_summary")
    async def send_delegation_summary(self, event: AstrMessageEvent) -> str:
        """Send a formatted delegation trace report to the user as an image or text message. The report is sent directly and does NOT enter conversation context.

        Call this tool exactly ONCE after all delegations are complete. It requires no arguments — the trace is recorded automatically during delegations.
        """
        if _delegation_depth.get() > 0:
            return "This tool can only be called by the top-level agent, not from within a delegated sub-task."

        already_sent = event.get_extra("_trace_summary_sent", False)
        if already_sent:
            return (
                "Delegation summary has already been sent. Do not call this tool again."
            )

        trace = event.get_extra(_DELEGATION_TRACE_KEY)
        if not trace:
            return "No delegation trace found for this event."

        try:
            rendered = await self._render_trace_image(trace)
            if rendered.startswith("http"):
                await event.send(MessageChain().url_image(rendered))
            else:
                await event.send(MessageChain().file_image(rendered))
        except Exception as exc:
            logger.warning("t2i rendering failed, falling back to text", exc_info=True)
            try:
                text = self._format_trace_text(trace)
                await event.send(MessageChain().message(text))
            except Exception:
                logger.error("Text fallback also failed", exc_info=True)
                return "Failed to send delegation summary. Please try again."

        event.set_extra("_trace_summary_sent", True)
        return "Delegation summary has been sent to the user."

    # ------------------------------------------------------------------ #
    # Chat Command: /agents
    # ------------------------------------------------------------------ #
    @filter.command("agents")
    async def cmd_list_agents(self, event: AstrMessageEvent):
        """List all available agents. / 列出所有可用的代理。"""
        orch = self.context.subagent_orchestrator
        if not orch or not orch.handoffs:
            yield event.plain_result(
                "No sub-agents configured. Only main agent is available."
            )
            return

        lines = ["Available agents:\n"]
        for h in orch.handoffs:
            desc = h.description or "(no description)"
            provider = h.provider_id or "default"
            tool_names = h.agent.tools
            if tool_names is None:
                tools_display = "all tools"
            elif not tool_names:
                tools_display = "none"
            else:
                tools_display = ", ".join(str(t) for t in tool_names)
            lines.append(
                f"- {h.agent.name}\n"
                f"  Provider: {provider}\n"
                f"  Description: {desc}\n"
                f"  Tools: {tools_display}"
            )
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Chat Command: /trace
    # ------------------------------------------------------------------ #
    @filter.command("trace")
    async def cmd_trace(self, event: AstrMessageEvent):
        """View the last delegation trace. / 查看上次委派追踪记录。"""
        umo = event.unified_msg_origin
        trace = self._last_traces.get(umo)
        if not trace:
            yield event.plain_result("No delegation trace found for this conversation.")
            return

        try:
            rendered = await self._render_trace_image(trace)
            yield event.image_result(rendered)
        except Exception:
            text = self._format_trace_text(trace)
            yield event.plain_result(text)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _find_handoff(
        self, handoffs: list[HandoffTool], name: str
    ) -> HandoffTool | None:
        """Find a HandoffTool by agent name (case-insensitive)."""
        name_lower = name.lower()
        for h in handoffs:
            if h.agent.name.lower() == name_lower:
                return h
        return None

    async def _invoke_main_agent(
        self,
        event: AstrMessageEvent,
        task: str,
        current_depth: int,
    ) -> str:
        """Delegate a task back to the main agent.

        Always excludes HandoffTools. Delegation tools (delegate_task_to_agent
        and list_available_agents) are kept when the next depth still has room
        to delegate, enabling multi-hop chains like boss -> secretary -> worker.
        """
        umo = event.unified_msg_origin
        prov_id = await self.context.get_current_chat_provider_id(umo)

        next_depth = current_depth + 1
        can_still_delegate = next_depth < self.max_delegation_depth

        toolset = ToolSet()
        for registered_tool in llm_tools.func_list:
            if isinstance(registered_tool, HandoffTool):
                continue
            if registered_tool.name == "send_delegation_summary":
                continue
            if not can_still_delegate and registered_tool.name in (
                "delegate_task_to_agent",
                "list_available_agents",
            ):
                continue
            if registered_tool.active:
                toolset.add_tool(registered_tool)

        cfg = self.context.get_config(umo=umo)
        prov_settings = cfg.get("provider_settings", {})
        max_steps = int(prov_settings.get("max_agent_step", 30))
        stream = prov_settings.get("streaming_response", False)

        prev_agent = _current_agent.get()
        prev_ct = _caller_target_counts.get()
        _current_agent.set(MAIN_AGENT_NAME)
        _caller_target_counts.set({})
        _delegation_depth.set(next_depth)
        try:
            llm_resp = await asyncio.wait_for(
                self.context.tool_loop_agent(
                    event=event,
                    chat_provider_id=prov_id,
                    prompt=task,
                    system_prompt="You are a helpful general-purpose assistant. Answer the question using available tools.",
                    tools=toolset if not toolset.empty() else None,
                    max_steps=max_steps,
                    stream=stream,
                ),
                timeout=self.delegation_timeout,
            )
            return llm_resp.completion_text or "(Main agent returned empty response)"
        except asyncio.TimeoutError:
            logger.error(
                "Delegation to main agent timed out after %ss",
                self.delegation_timeout,
            )
            return (
                f"{_ERROR_PREFIX} Main agent timed out after {self.delegation_timeout}s. "
                f"Please try to answer directly."
            )
        except Exception as e:
            logger.error("Failed to delegate task to main agent", exc_info=True)
            return (
                f"{_ERROR_PREFIX} Delegation to main agent failed: {e}. "
                f"Please try to answer directly."
            )
        finally:
            _current_agent.set(prev_agent)
            _caller_target_counts.set(prev_ct)
            _delegation_depth.set(current_depth)

    async def _invoke_subagent(
        self,
        event: AstrMessageEvent,
        handoff: HandoffTool,
        task: str,
        current_depth: int,
    ):
        """Invoke a sub-agent: build its toolset, resolve provider, prepare
        dialog context, then run a full tool-loop agent cycle."""
        next_depth = current_depth + 1
        can_still_delegate = next_depth < self.max_delegation_depth
        toolset = self._build_toolset(
            handoff.agent.tools, can_delegate=can_still_delegate
        )

        umo = event.unified_msg_origin
        prov_id = (
            handoff.provider_id or await self.context.get_current_chat_provider_id(umo)
        )

        contexts = None
        if handoff.agent.begin_dialogs:
            contexts = []
            for dialog in handoff.agent.begin_dialogs:
                try:
                    if isinstance(dialog, Message):
                        contexts.append(dialog)
                    else:
                        contexts.append(Message.model_validate(dialog))
                except Exception:
                    continue

        cfg = self.context.get_config(umo=umo)
        prov_settings = cfg.get("provider_settings", {})
        max_steps = int(prov_settings.get("max_agent_step", 30))
        stream = prov_settings.get("streaming_response", False)

        prev_agent = _current_agent.get()
        prev_ct = _caller_target_counts.get()
        _current_agent.set(handoff.agent.name.lower())
        _caller_target_counts.set({})
        _delegation_depth.set(current_depth + 1)
        try:
            return await asyncio.wait_for(
                self.context.tool_loop_agent(
                    event=event,
                    chat_provider_id=prov_id,
                    prompt=task,
                    system_prompt=handoff.agent.instructions,
                    tools=toolset,
                    contexts=contexts,
                    max_steps=max_steps,
                    stream=stream,
                ),
                timeout=self.delegation_timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Agent '{handoff.agent.name}' timed out after {self.delegation_timeout}s"
            )
        finally:
            _current_agent.set(prev_agent)
            _caller_target_counts.set(prev_ct)
            _delegation_depth.set(current_depth)

    async def _render_trace_image(self, trace: list[dict]) -> str:
        """Render the delegation trace as an image via html_render."""
        prepared = []
        for entry in trace:
            task_text = _html.escape(entry["task"])
            resp_text = _html.escape(entry["response"])
            prepared.append(
                {
                    **entry,
                    "time_str": datetime.datetime.fromtimestamp(
                        entry["timestamp"]
                    ).strftime("%H:%M:%S"),
                    "caller": _html.escape(str(entry["caller"])),
                    "target": _html.escape(str(entry["target"])),
                    "task_short": (
                        task_text[:200] + "..." if len(task_text) > 200 else task_text
                    ),
                    "response_short": (
                        resp_text[:300] + "..." if len(resp_text) > 300 else resp_text
                    ),
                }
            )
        return await self.html_render(
            _TRACE_HTML_TEMPLATE, {"trace": prepared}, return_url=True
        )

    @staticmethod
    def _format_trace_text(trace: list[dict]) -> str:
        """Fallback: format delegation trace as plain text."""
        lines = ["===== Delegation Trace Report =====\n"]
        for i, entry in enumerate(trace, 1):
            ts = datetime.datetime.fromtimestamp(entry["timestamp"]).strftime(
                "%H:%M:%S"
            )
            status = "OK" if entry["status"] == "success" else "ERROR"
            task_text = entry["task"][:200]
            resp_text = entry["response"][:300]
            lines.append(
                f"[Step {i}] {ts} | depth={entry['depth']} | {status}\n"
                f"  {entry['caller']} -> {entry['target']}\n"
                f"  Task: {task_text}\n"
                f"  Response: {resp_text}\n"
            )
        lines.append(f"Total steps: {len(trace)}")
        return "\n".join(lines)

    @staticmethod
    def _build_toolset(
        tools: list | None, *, can_delegate: bool = True
    ) -> ToolSet | None:
        """Build a ToolSet for the sub-agent based on its tool configuration.

        - tools=None  -> all registered tools (except HandoffTools),
                         includes delegate_task_to_agent for cross-agent calls
        - tools=[]    -> no tools
        - tools=[...] -> only the specified tools

        When *can_delegate* is False, delegation tools are excluded regardless
        of the tool list so the LLM doesn't waste a call that would be rejected.
        """
        _delegation_tool_names = {"delegate_task_to_agent", "list_available_agents"}

        if tools is None:
            toolset = ToolSet()
            for registered_tool in llm_tools.func_list:
                if isinstance(registered_tool, HandoffTool):
                    continue
                if registered_tool.name == "send_delegation_summary":
                    continue
                if not can_delegate and registered_tool.name in _delegation_tool_names:
                    continue
                if registered_tool.active:
                    toolset.add_tool(registered_tool)
            return None if toolset.empty() else toolset

        if not tools:
            return None

        toolset = ToolSet()
        for tool_name_or_obj in tools:
            if isinstance(tool_name_or_obj, str):
                if not can_delegate and tool_name_or_obj in _delegation_tool_names:
                    continue
                registered_tool = llm_tools.get_func(tool_name_or_obj)
                if registered_tool and registered_tool.active:
                    toolset.add_tool(registered_tool)
            elif isinstance(tool_name_or_obj, FunctionTool):
                if not can_delegate and tool_name_or_obj.name in _delegation_tool_names:
                    continue
                toolset.add_tool(tool_name_or_obj)
        return None if toolset.empty() else toolset
