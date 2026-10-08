"""
Exercise the MCP server without a model.

Same idea as check_retrieval.py: test one layer in isolation before adding
the non-deterministic part on top. This connects as an MCP client, lists the
tools, and calls them directly as two different users.

If the agent later gives a wrong answer, this is how you rule out the tool
layer before blaming the model.

    python src/check_mcp.py
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from agent_mcp import server_params


async def session_for(user_id: str, role: str):
    return stdio_client(server_params(user_id, role))


async def probe(user_id: str, role: str, calls: list):
    print("=" * 78)
    print(f"SESSION: {user_id} / {role}")
    print("=" * 78)

    async with stdio_client(server_params(user_id, role)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            listed = await session.list_tools()
            print(f"  tools advertised: {len(listed.tools)}")
            for t in listed.tools:
                fields = list(t.input_schema.get("properties", {}))
                print(f"    - {t.name:<20} args: {fields}")

            # The check that matters: no tool anywhere takes an identity.
            leaks = [t.name for t in listed.tools
                     if {"user", "user_id", "role"} &
                     set(t.input_schema.get("properties", {}))]
            print(f"  identity fields exposed to the model: "
                  f"{leaks if leaks else 'none'}")
            print()

            for name, args in calls:
                out = await session.call_tool(name, args)
                payload = json.loads(out.content[0].text)
                ev = payload.pop("_trace", {})
                print(f"  {name}({args})")
                print(f"    outcome : {ev.get('outcome')}")
                print(f"    summary : {ev.get('result_summary')}")
                if ev.get("outcome") == "ok":
                    res = payload.get("result")
                    if isinstance(res, list) and res:
                        has_contact = "contact_name" in res[0]
                        print(f"    contact fields present in rows: "
                              f"{has_contact}")
                print()


CALLS = [
    ("incident_stats", {"severity": "Sev-1"}),
    ("list_incidents", {"service": "payments", "limit": 3}),
    ("get_oncall_contact", {"service": "payments"}),
]


async def main():
    await probe("p.zinzuvadia", "engineer", CALLS)
    await probe("ext.contractor", "contractor", CALLS)


if __name__ == "__main__":
    asyncio.run(main())
