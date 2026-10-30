"""Agent 1: Claude + yfinance MCP tools. No skills.

    python agent_plain.py "How has TCS.NS performed over the past year?"

Needs the MCP server running:  python yfinance_server.py   (http://127.0.0.1:8000/mcp)
Optional LangSmith tracing:    export LANGSMITH_TRACING=true LANGSMITH_API_KEY=... LANGSMITH_PROJECT=yfinance-agent
"""

import asyncio
import os
import sys
from datetime import datetime

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

# ---------------------------------------------------------------- configuration
MCP_URL = os.environ.get("YF_MCP_URL", "http://127.0.0.1:8000/mcp")

options = ClaudeAgentOptions(
    system_prompt=(
        "You are a market-data analyst. Use the yfinance tools for every number you report; "
        "never recall prices or fundamentals from memory. If a tool returns an error or no data, say so plainly."
    ),
    # the running FastMCP server; tools become mcp__yfinance__<tool_name>
    mcp_servers={"yfinance": {"type": "http", "url": MCP_URL}},
    # MCP tool calls need approval; this allow rule approves every tool of the yfinance server
    allowed_tools=["mcp__yfinance__*"],
    # do not read .claude/ from disk -> no skills discovered. Without this line the SDK
    # would find .claude/skills/ in the working directory and this agent would use them.
    setting_sources=[],
    max_turns=20,
)

# ---------------------------------------------------------------- run
prompt = " ".join(sys.argv[1:]) or "Give me a snapshot of RELIANCE.NS"
os.makedirs("logs", exist_ok=True)
log_file = open(f"logs/plain_{datetime.now():%Y%m%d_%H%M%S}.log", "w", encoding="utf-8")


def log(event: str):
    line = f"{datetime.now():%H:%M:%S}  {event}"
    print(line)
    log_file.write(line + "\n")


async def main():
    log(f"PROMPT       {prompt}")
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():

            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                log(
                    f"SESSION      model={msg.data.get('model')}  mcp={msg.data.get('mcp_servers')}"
                )
                log(f"SKILLS       {msg.data.get('skills') or 'none'}")

            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        log(f"TOOL CALL    {block.name}  {block.input}")

            elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        size = len(str(block.content))
                        log(
                            f"TOOL RESULT  {'ERROR' if block.is_error else 'ok'}  {size} chars"
                        )

            elif isinstance(msg, ResultMessage):
                u = msg.usage or {}
                log(
                    f"DONE         turns={msg.num_turns}  cost=${msg.total_cost_usd or 0:.4f}  "
                    f"tokens: in={u.get('input_tokens', 0)} out={u.get('output_tokens', 0)} "
                    f"cache_read={u.get('cache_read_input_tokens', 0)}"
                )
                answer = msg.result or ""

    print("\n" + "=" * 80 + "\n" + answer + "\n" + "=" * 80)
    log_file.write("\n" + answer + "\n")
    log_file.close()


asyncio.run(main())