import os
import sys

from anthropic import Anthropic

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

question = (
    " ".join(sys.argv[1:])
    or "What is the current share price of Reliance Industries (RELIANCE.NS)?"
)

response = client.messages.create(
    model=MODEL,
    max_tokens=600,
    system=(
        "You are a market-data analyst. Answer questions about listed companies "
        "with the latest price, market cap and P/E ratio."
    ),
    messages=[{"role": "user", "content": question}],
)

# The reply is a list of blocks; some models add a 'thinking' block before the 'text' block.
answer = "".join(block.text for block in response.content if block.type == "text")
print(answer)

print("-" * 60)
print(f"model={response.model}  stop={response.stop_reason}")
print(
    f"tokens: input={response.usage.input_tokens}  output={response.usage.output_tokens}"
)