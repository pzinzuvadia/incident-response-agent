"""
The same five tools, exposed over MCP instead of called as Python functions.

WHY THIS EXISTS AT ALL.
   Locally it is unnecessary. The tools read a SQLite file and a Chroma
   store sitting on the same disk as the agent; wrapping them in a protocol
   so a process can talk to itself buys nothing. It is here because the
   authorization argument is weak until the tools run somewhere else.

   When the tool and the caller are the same process, passing a UserContext
   safely is trivial — you just pass it. The hard version of the problem only
   appears at a boundary, and that is where every real system lives.

THE PROBLEM MCP DOES NOT SOLVE FOR YOU.
   MCP describes tools, arguments and results. It has no concept of a caller.
   A server receives "call list_incidents with service=payments". It does not
   receive "...on behalf of this authenticated person". The protocol is silent
   on identity, which means you have to decide where identity comes from, and
   it is easy to decide badly.

   THE WRONG ANSWER: add `user` and `role` to each tool's input schema. Now
   the model supplies them. A field the model fills is a field the model can
   fill with anything, and your access control is a suggestion.

   THE ANSWER HERE: identity is bound when the server process starts, from
   the environment the client supplied, before any tool is listed and long
   before the model sees anything. One UserContext per session. It appears in
   no schema, so the model cannot read it, set it, or argue with it.

   This is the same shape as a gateway that validates a token and hands the
   backend an already-authenticated principal. The backend does not ask the
   request who it is.

WHAT THE SERVER RECORDS.
   Every call is written to the agent_traces table in data/traces.db before
   the result goes back, including the ones that were refused. The trace is
   the server's own record of what it did, not the client's report of what it
   thinks happened. Each result also carries that event inline so the client
   can render it live; the model never sees it, because it is stripped before
   the result reaches the conversation.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from tools import TOOL_SCHEMAS, TOOL_FUNCTIONS, UserContext
from trace import AccessDenied, TraceEvent, TRACE, TRACE_DB

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Identity, bound once, at startup.
#
# Not a tool argument. Not in any schema. Not reachable by the model. If the
# client did not supply a role, the server does not start — failing closed is
# the only safe default when the whole point of the process is to enforce a
# boundary.
# ---------------------------------------------------------------------------

def bind_identity() -> UserContext:
    user_id = os.environ.get("INCIDENT_AGENT_USER")
    role = os.environ.get("INCIDENT_AGENT_ROLE")

    if not user_id or not role:
        print(
            "refusing to start: INCIDENT_AGENT_USER and INCIDENT_AGENT_ROLE "
            "must both be set by the client that spawns this server.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    print(f"[mcp_server] identity bound: {user_id} / {role}", file=sys.stderr)
    return UserContext(user_id=user_id, role=role)


SESSION_CTX = bind_identity()

server = Server("incident-tools")


async def list_tools(ctx, params) -> types.ListToolsResult:
    """Advertise the five tools.

    These are the same schemas the direct-call agent uses, written by hand
    rather than derived from Python signatures — the descriptions are load
    bearing and deriving them would throw away the part that does the work.

    Note what is absent: no tool takes a user or a role. There is nothing
    here for the model to spoof.
    """
    return types.ListToolsResult(tools=[
        types.Tool(
            name=s["name"],
            description=s["description"],
            inputSchema=s["input_schema"],
        )
        for s in TOOL_SCHEMAS
    ])


async def call_tool(ctx, params) -> types.CallToolResult:
    """Execute one tool against the session's bound identity.

    `params.arguments` comes from the model and is treated as untrusted
    input: it decides what is asked for, never who is asking.
    """
    name = params.name
    arguments = params.arguments or {}

    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return types.CallToolResult(
            is_error=True,
            content=[types.TextContent(
                type="text",
                text=json.dumps({"error": "no_such_tool", "message": name}))],
        )

    start = time.perf_counter()
    try:
        result = fn(SESSION_CTX, **arguments)
        outcome, summary = "ok", _summarise(name, result)
        payload = {"result": result}
    except AccessDenied as exc:
        outcome, summary = "denied", str(exc)
        payload = {
            "error": "access_denied",
            "message": str(exc),
            "guidance": "Tell the user they are not authorised to see this.",
        }
    except Exception as exc:
        outcome, summary = "error", f"{type(exc).__name__}: {exc}"
        payload = {"error": type(exc).__name__, "message": str(exc)}

    # The tool is already wrapped in @traced, so by the time execution reaches
    # here the call has been recorded in memory and written to the traces
    # table. The server does not record it a second time; it reads back what
    # was recorded and sends it along so the client can render it live.
    #
    # One writer, one definition of what a recorded call looks like. Two
    # writers is how you end up with two audit trails that disagree.
    event = TRACE.events[-1] if TRACE.events else TraceEvent(
        tool=name, user=SESSION_CTX.user_id, role=SESSION_CTX.role,
        arguments=arguments,
        duration_ms=(time.perf_counter() - start) * 1000,
        outcome=outcome, result_summary=summary,
    )

    payload["_trace"] = event.__dict__
    return types.CallToolResult(
        content=[types.TextContent(type="text",
                                   text=json.dumps(payload, default=str))],
    )


server.add_request_handler("tools/list", types.PaginatedRequestParams,
                           list_tools)
server.add_request_handler("tools/call", types.CallToolRequestParams,
                           call_tool)


def _summarise(name: str, result) -> str:
    if isinstance(result, list):
        return f"{len(result)} item(s)"
    if isinstance(result, dict):
        if "incident_count" in result:
            return (f"n={result['incident_count']}, "
                    f"median={result['median_resolution_minutes']}min")
        if "contact_name" in result:
            return f"{result.get('owning_team')} / {result['contact_name']}"
        if "incident_id" in result:
            return result["incident_id"]
        return "ok" if result else "empty"
    return str(result)[:60]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
