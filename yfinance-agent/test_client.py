import asyncio
import json
import os

from fastmcp import Client

MCP_URL = os.environ.get("YF_MCP_URL", "http://127.0.0.1:8000/mcp")
TICKER = "AAPL"


def parse(result):
    raw = result.content[0].text
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"  server said: {raw}")
        return None


async def main():
    async with Client(MCP_URL) as client:
        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools], "\n")

        r = await client.call_tool(
            "get_historical_stock_prices", {"ticker": TICKER, "period": "5d"}
        )
        if rows := parse(r):
            print(
                f"get_historical_stock_prices -> {len(rows)} rows, last close={rows[-1]['Close']:.2f}"
            )

        r = await client.call_tool("get_stock_info", {"ticker": TICKER})
        if info := parse(r):
            print(
                f"get_stock_info -> {info.get('longName')} | price={info.get('currentPrice')}"
            )

        r = await client.call_tool("get_yahoo_finance_news", {"ticker": TICKER})
        if news := parse(r):
            print(
                f"get_yahoo_finance_news -> {len(news)} articles, first: {news[0]['title']}"
            )

        r = await client.call_tool("get_stock_actions", {"ticker": TICKER})
        if actions := parse(r):
            print(f"get_stock_actions -> {len(actions)} events, latest: {actions[-1]}")


asyncio.run(main())