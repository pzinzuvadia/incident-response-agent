"""
The same agent, with the tools on the other side of a process boundary.

Run this against agent.py and compare. The model is the same, the prompt is
the same, the five tools are the same, and the answers are the same. What
changes is the one thing that matters:

   agent.py      ctx = UserContext(user, role)   →  fn(ctx, **args)
                 Identity is an argument the loop passes. Safe because the
                 loop and the tool are the same program.

   agent_mcp.py  env INCIDENT_AGENT_ROLE=...     →  spawn server
                 Identity is a property of the session, fixed before the
                 server will answer anything. The loop cannot pass it per
                 call even if it wanted to, and neither can the model.

The second is the one that survives contact with a real deployment, because
in a real deployment the thing calling the tool and the thing enforcing the
rule are not the same program and do not trust each other.

WHAT GOT WORSE.
   This file is longer and it is async. Tool discovery is now a round trip
   instead of an import. There is a subprocess to manage and a failure mode
   where it dies and the agent has no tools at all. That is the honest cost
   of the boundary, and it is only worth paying when you actually have one.
"""

import asyncio
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path

from dotenv import load_dotenv
from anthropic import Anthropic

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trace import Trace, TraceEvent, SESSION_ID, persist_session

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MODEL = os.getenv("MODEL", "claude-sonnet-4-5-20250929")
MAX_ITERATIONS = 6

# Imported rather than duplicated: the prompt is not where access control
# lives, so it does not change when the transport does.
from agent import SYSTEM_PROMPT  # noqa: E402


def server_params(user_id: str, role: str) -> StdioServerParameters:
    """Describe the server process, including the identity it will run as.

    This is the whole authorization handoff, and it happens here: before the
    server starts, before tools are listed, before the model is called. The
    client asserts who the caller is, once, out of band.

    In production this assertion is not a bare environment variable. It is a
    validated token, and the server verifies it rather than believing it.
    The structure is the same either way: the caller's identity arrives
    through a channel the model is not part of.
    """
    env = dict(os.environ)
    env["INCIDENT_AGENT_USER"] = user_id
    env["INCIDENT_AGENT_ROLE"] = role
    # The correlation id travels with the identity. The tools run in that
    # subprocess, so without this the server mints its own id and the trace
    # rows never join to the session row this process writes.
    env["INCIDENT_AGENT_SESSION"] = SESSION_ID
    return StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().parent / "mcp_server.py")],
        env=env,
    )


async def ask(question: str, user_id: str, role: str,
              verbose: bool = False) -> tuple[str, Trace]:
    client = Anthropic()
    trace = Trace()
    started = time.perf_counter()

    def finish(answer: str, iterations: int):
        persist_session(
            user_id=user_id, role=role, question=question, answer=answer,
            transport="mcp", tool_calls=len(trace.events),
            iterations=iterations,
            total_ms=(time.perf_counter() - started) * 1000,
        )
        return answer, trace

    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(
            stdio_client(server_params(user_id, role)))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        # Tool discovery is now a protocol call. The agent learns what it can
        # do at runtime instead of at import time, which is the actual reason
        # MCP exists: the client does not need to know the tools in advance.
        listed = await session.list_tools()
        schemas = [
            {"name": t.name,
             "description": t.description,
             "input_schema": t.input_schema}
            for t in listed.tools
        ]

        messages = [{"role": "user", "content": question}]

        for iteration in range(MAX_ITERATIONS):
            response = client.messages.create(
                model=MODEL,
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                tools=schemas,
                messages=messages,
            )

            if response.stop_reason != "tool_use":
                return finish("".join(b.text for b in response.content
                                      if b.type == "text"), iteration + 1)

            messages.append({"role": "assistant", "content": response.content})

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if verbose:
                    print(f"  → {block.name}({block.input})")

                out = await session.call_tool(block.name, block.input)
                payload = json.loads(out.content[0].text)

                # The server's own record of the call, lifted out before the
                # result reaches the conversation. The model is told what it
                # got; it is not told how it was logged.
                recorded = payload.pop("_trace", None)
                if recorded:
                    trace.record(TraceEvent(**recorded))

                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(payload, default=str),
                })

            messages.append({"role": "user", "content": results})

        return finish("Stopped after the maximum number of tool calls "
                      "without reaching an answer.", MAX_ITERATIONS)


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Ask the incident assistant, with tools served over MCP.")
    parser.add_argument("question", nargs="+")
    parser.add_argument("--user", default="p.zinzuvadia")
    parser.add_argument("--role", default="engineer",
                        help="engineer | incident_commander | contractor")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    question = " ".join(args.question)
    print(f"\n\033[1mQ [{args.user} / {args.role}] via MCP:\033[0m "
          f"{question}\n")

    answer, trace = asyncio.run(
        ask(question, args.user, args.role, verbose=args.verbose))
    print(answer)

    if args.trace:
        print("\n" + "=" * 78)
        print("TRACE  (recorded server-side, persisted to data/traces.db)")
        print("=" * 78)
        print(trace.render())
    print()


if __name__ == "__main__":
    main()
