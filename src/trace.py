"""
Trace layer.

Traditional monitoring answers "is the service up". That question is useless
for an agent, because an agent can be completely healthy and still have
reached its answer through the wrong tools, on the wrong data, for the wrong
user.

So every tool call records: who asked, which tool, what arguments, how long it
took, what came back, and whether it was refused. That record is the only
honest way to answer "why did it say that", and it is the thing you need when
somebody disputes an answer three weeks later.

This is deliberately boring. A list of dictionaries and a decorator. The point
is not the implementation, it is that the boundary where tools are called is
the only place you can capture this, so it has to be built in at that boundary
rather than bolted on afterwards.
"""

import datetime as _dt
import functools
import json
import os as _os
import sqlite3
import sys as _sys
import time
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TraceEvent:
    tool: str
    user: str
    role: str
    arguments: dict
    duration_ms: float
    outcome: str          # "ok" | "denied" | "error"
    result_summary: str


@dataclass
class Trace:
    events: list = field(default_factory=list)

    def record(self, event: TraceEvent):
        self.events.append(event)

    def clear(self):
        self.events = []

    def render(self) -> str:
        if not self.events:
            return "(no tool calls)"

        lines = []
        header = (f"{'#':<3} {'TOOL':<20} {'USER':<12} {'OUTCOME':<9} "
                  f"{'MS':>7}  ARGUMENTS")
        lines.append(header)
        lines.append("-" * len(header))
        for i, e in enumerate(self.events, 1):
            args = ", ".join(f"{k}={v!r}" for k, v in e.arguments.items()
                             if v is not None)
            lines.append(f"{i:<3} {e.tool:<20} {e.user:<12} {e.outcome:<9} "
                         f"{e.duration_ms:>7.1f}  {args[:60]}")
            lines.append(f"{'':<3} {'└─ ' + e.result_summary[:88]}")
        total = sum(e.duration_ms for e in self.events)
        lines.append("-" * len(header))
        lines.append(f"{len(self.events)} tool call(s), {total:.1f} ms total "
                     f"in tools")
        return "\n".join(lines)


# One trace per process is enough for a single-user CLI demo. In a real
# deployment this would be per-request and carry a correlation id.
TRACE = Trace()


# ---------------------------------------------------------------------------
# Persistence.
#
# Printing a trace is enough to debug a run you are watching. It is not enough
# to answer "why did it say that" three weeks later, when the run is long gone
# and somebody is disputing the answer. Persisting is what turns a debugging
# aid into an audit record.
#
# A table rather than a log file, because the questions people actually ask of
# this are queries: show me every refusal, show me which tool is slowest, show
# me everything one user did last Tuesday. Grepping a log answers none of those
# well.
#
# It is a SEPARATE database from the incident data, deliberately. Two reasons.
# generate_data.py recreates incidents.db, and an audit record that a dev
# script can wipe is not an audit record. And operational telemetry about the
# system is not business data held by the system; giving them one lifecycle
# means they get one retention policy, one backup, one access rule, and those
# should almost never be the same.
# ---------------------------------------------------------------------------

TRACE_DB = Path(__file__).resolve().parent.parent / "data" / "traces.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_traces (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,
    session_id     TEXT    NOT NULL,
    user_id        TEXT    NOT NULL,
    role           TEXT    NOT NULL,
    tool           TEXT    NOT NULL,
    arguments      TEXT    NOT NULL,
    outcome        TEXT    NOT NULL,
    duration_ms    REAL    NOT NULL,
    result_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_traces_outcome ON agent_traces(outcome);
CREATE INDEX IF NOT EXISTS idx_traces_user    ON agent_traces(user_id);
CREATE INDEX IF NOT EXISTS idx_traces_ts      ON agent_traces(ts);

CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id  TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    role        TEXT NOT NULL,
    transport   TEXT NOT NULL,
    question    TEXT NOT NULL,
    answer      TEXT,
    tool_calls  INTEGER,
    iterations  INTEGER,
    total_ms    REAL
);
"""

# One id per question, so the calls belonging to a single answer can be read
# back together. Without it the table is a pile of calls with no notion of
# which ones belonged to the same question.
#
# It is inherited from the environment when the caller supplies one. That
# matters for the MCP version: the tools run in a subprocess, so without this
# the server would mint its own id and the trace rows would never join to the
# session row written by the client. Propagating a correlation id across a
# process boundary is the oldest trick in distributed tracing and it is just
# as necessary here.
SESSION_ID = _os.environ.get("INCIDENT_AGENT_SESSION") or _uuid.uuid4().hex[:12]


def persist(event: "TraceEvent", db_path=None) -> None:
    """Write one trace event to the agent_traces table.

    Best effort. If the trace store is unavailable the tool call has already
    succeeded, and failing the user's request because the audit write failed
    would be the wrong trade in this system. In one where the audit record is
    a compliance requirement it would be the right trade, and this would
    raise instead.
    """
    path = Path(db_path or TRACE_DB)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO agent_traces (ts, session_id, user_id, role, "
                "tool, arguments, outcome, duration_ms, result_summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                    SESSION_ID,
                    event.user,
                    event.role,
                    event.tool,
                    json.dumps(event.arguments, default=str),
                    event.outcome,
                    round(event.duration_ms, 2),
                    event.result_summary,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[trace] could not persist: {exc}", file=_sys.stderr)


def persist_session(user_id: str, role: str, question: str, answer: str,
                    transport: str, tool_calls: int, iterations: int,
                    total_ms: float, db_path=None) -> None:
    """Record the question that caused a set of tool calls, and the answer.

    WHY THIS IS A SECOND TABLE AND NOT MORE COLUMNS.
       The grain is different. One question produces several tool calls, so
       putting the question and answer on every trace row would repeat them
       three or four times per question and leave you deduplicating at read
       time. One row per question here, one row per call there, joined on
       session_id.

    WHY IT IS WRITTEN LAST, AND WHY IT IS LESS TRUSTWORTHY.
       The trace rows are written by the tool layer, at the point where data
       is actually reached. This row is written by the agent loop, after the
       fact, by the component being observed. That is a weaker position: if
       the loop crashes mid-question the tool calls are already on disk and
       this row never appears.

       That asymmetry is deliberate and worth keeping. If the two ever
       disagree, believe the trace rows. An orphaned set of trace rows with
       no session row is not a bug in the logging, it is the logging telling
       you a run died partway through.
    """
    path = Path(db_path or TRACE_DB)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO agent_sessions (session_id, ts, "
                "user_id, role, transport, question, answer, tool_calls, "
                "iterations, total_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    SESSION_ID,
                    _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                    user_id,
                    role,
                    transport,
                    question,
                    answer,
                    tool_calls,
                    iterations,
                    round(total_ms, 1),
                ),
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[trace] could not persist session: {exc}", file=_sys.stderr)


class AccessDenied(Exception):
    """Raised inside a tool when the caller is not permitted to reach data.

    Deliberately an exception rather than a silent empty result: a refusal is
    an event worth recording, and the agent should be told clearly that it was
    refused rather than being allowed to assume the data does not exist.
    """


def _record(event: TraceEvent) -> None:
    """In-memory for this run, and on disk for every run after it.

    Both agents go through here, so there is exactly one place that decides
    what a recorded call looks like. A second writer somewhere else is how
    you end up with two subtly different audit trails.
    """
    TRACE.record(event)
    persist(event)


def traced(summarise):
    """Wrap a tool so every call is recorded.

    `summarise` turns the return value into one short line for the trace.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(ctx, **kwargs):
            start = time.perf_counter()
            try:
                result = fn(ctx, **kwargs)
            except AccessDenied as exc:
                _record(TraceEvent(
                    tool=fn.__name__,
                    user=ctx.user_id,
                    role=ctx.role,
                    arguments=kwargs,
                    duration_ms=(time.perf_counter() - start) * 1000,
                    outcome="denied",
                    result_summary=str(exc),
                ))
                raise
            except Exception as exc:
                _record(TraceEvent(
                    tool=fn.__name__,
                    user=ctx.user_id,
                    role=ctx.role,
                    arguments=kwargs,
                    duration_ms=(time.perf_counter() - start) * 1000,
                    outcome="error",
                    result_summary=f"{type(exc).__name__}: {exc}",
                ))
                raise

            _record(TraceEvent(
                tool=fn.__name__,
                user=ctx.user_id,
                role=ctx.role,
                arguments=kwargs,
                duration_ms=(time.perf_counter() - start) * 1000,
                outcome="ok",
                result_summary=summarise(result),
            ))
            return result
        return wrapper
    return decorator
