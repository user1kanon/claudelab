import json
import sys

import yfinance as yf
from fastmcp import FastMCP

mcp = FastMCP(
    name="yfinance",
    instructions="Tools for fetching stock data from Yahoo Finance by ticker symbol (e.g. AAPL, RELIANCE.NS).",
)


@mcp.tool
def get_historical_stock_prices(
    ticker: str, period: str = "1mo", interval: str = "1d"
) -> str:
    """Get historical OHLCV data for a stock.

    Args:
        ticker: Ticker symbol, e.g. "AAPL" or "TCS.NS".
        period: 1d,5d,1mo,3mo,6mo,1y,2y,5y,10y,ytd,max (default 1mo).
        interval: 1m,2m,5m,15m,30m,60m,90m,1h,1d,5d,1wk,1mo,3mo (default 1d).
                  Intraday intervals are limited to the last 60 days.
    Returns JSON list of records: Date, Open, High, Low, Close, Volume, Dividends, Stock Splits.
    """
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval)
        if df.empty:
            return f"No price data found for {ticker} (period={period}, interval={interval})."
        return df.reset_index(names="Date").to_json(orient="records", date_format="iso")
    except Exception as e:
        return f"Error fetching historical prices for {ticker}: {e}"


@mcp.tool
def get_stock_info(ticker: str) -> str:
    """Get comprehensive stock data: current price, valuation metrics, margins,
    dividends, balance-sheet summary, analyst targets and company details.

    Args:
        ticker: Ticker symbol, e.g. "AAPL".
    Returns JSON object of all fields Yahoo Finance exposes for the ticker.
    """
    try:
        info = yf.Ticker(ticker).info
        if not info or "symbol" not in info:
            return f"No stock info found for {ticker}."
        return json.dumps(info, default=str)
    except Exception as e:
        return f"Error fetching stock info for {ticker}: {e}"


@mcp.tool
def get_yahoo_finance_news(ticker: str) -> str:
    """Get the latest news articles for a stock.

    Args:
        ticker: Ticker symbol, e.g. "AAPL".
    Returns JSON list of {title, summary, publisher, published, url}.
    """
    try:
        news = yf.Ticker(ticker).news
        items = []
        for n in news or []:
            c = n.get("content", {})
            if c.get("contentType") != "STORY":
                continue
            items.append(
                {
                    "title": c.get("title"),
                    "summary": c.get("summary"),
                    "publisher": c.get("provider", {}).get("displayName"),
                    "published": c.get("pubDate"),
                    "url": c.get("canonicalUrl", {}).get("url"),
                }
            )
        if not items:
            return f"No news found for {ticker}."
        return json.dumps(items)
    except Exception as e:
        return f"Error fetching news for {ticker}: {e}"


@mcp.tool
def get_stock_actions(ticker: str) -> str:
    """Get dividend and stock-split history for a stock.

    Args:
        ticker: Ticker symbol, e.g. "AAPL".
    Returns JSON list of records: Date, Dividends, Stock Splits.
    """
    try:
        df = yf.Ticker(ticker).actions
        if df is None or df.empty:
            return f"No dividend/split history found for {ticker}."
        return df.reset_index(names="Date").to_json(orient="records", date_format="iso")
    except Exception as e:
        return f"Error fetching stock actions for {ticker}: {e}"


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--stdio" in args:
        mcp.run()
    elif "--https" in args:
        i = args.index("--https")
        cert, key = args[i + 1], args[i + 2]
        mcp.run(
            transport="http",
            host="127.0.0.1",
            port=8000,
            uvicorn_config={"ssl_certfile": cert, "ssl_keyfile": key},
        )
    else:
        mcp.run(transport="http", host="127.0.0.1", port=8000)