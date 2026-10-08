# Incident Response Agent

![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**Building the agent took an afternoon. Everything around it took the rest of the time, and that part is the point.**

An agent that answers questions about production incidents by reading across two kinds of data at once: 200 structured incident records and 14 written postmortems. It runs locally on synthetic data, with no cloud account and no framework.

The incident domain is a vehicle. What the repo is actually about is the harness around the model — retrieval, tool design, authorization, and tracing which is where the engineering turned out to live.

## Quick start

Python 3.10+ and an Anthropic API key.

```bash
git clone https://github.com/pzinzuvadia/incident-response-agent.git
cd incident-response-agent
```
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                    # add ANTHROPIC_API_KEY

python generate_data.py                 # 200 incidents → SQLite
python src/ingest.py                    # 14 postmortems → vector store
python src/agent.py "We're seeing payment timeouts again. Has this happened before, and who should I call?" --trace
```

First run of `ingest.py` downloads an ~80MB embedding model. Detailed setup is in [§5](#).

## What that produces

```
Q [p.zinzuvadia / engineer]: We're seeing payment timeouts again.
Has this happened before, and who should I call?

Yes — this is a recurring failure mode in payments, not a new problem.

- INC-0020 (Nov 14, 2025, Sev-3) — connection pool sized for traffic from
  18 months prior, couldn't handle morning peak.
- INC-0076 (Jan 29, 2026, Sev-2) — p99 degraded to 6s. Same failure mode.
- INC-0100 (Mar 18, 2026, Sev-1) — ~4,100 checkouts failed. Third
  occurrence of the same mode.
- INC-0144 (Jun 3, 2026, Sev-2) — driver version bump changed idle timeout
  defaults, shrinking the effective pool over time.
- INC-0187 (Aug 21, 2026, Sev-2) — request queueing under sustained traffic.

From the INC-0100 postmortem: "Holding a connection while waiting on a
third party is the actual anti-pattern here. The timeout you set on an
external call is also, implicitly, a decision about how long you are
willing to hold every resource behind it."

[...]

Who to call: Payments Platform — Dana Whitfield, +1-415-555-0142

==============================================================================
TRACE
==============================================================================
#   TOOL                 USER            OUTCOME        MS  ARGUMENTS
---------------------------------------------------------------------------
1   search_postmortems   p.zinzuvadia    ok          363.6  query='payment timeouts', service='payments'
    └─ 10 chunk(s) from 4 postmortem(s)
2   list_incidents       p.zinzuvadia    ok            3.7  service='payments'
    └─ 48 incident(s)
3   get_oncall_contact   p.zinzuvadia    ok            0.1  service='payments'
    └─ Payments Platform / Dana Whitfield
---------------------------------------------------------------------------
3 tool call(s), 367.4 ms total in tools
```

Three things in that output are worth noticing before anything else.

**It found a ten-month pattern that no single document states.** Those five incidents were written months apart by different people, and none of them mention "connection pool" in a title or summary. That is a property of how the documents were chunked at ingestion, not of how the question was phrased.

**Retrieval only reached four of the five.** The fifth incident got into the answer through `list_incidents`, not through search. Giving the agent both a retrieval tool and a query tool means retrieval does not have to be perfect.

**The same question from a contractor returns the same history and no phone number.** The refusal happens inside the tool, not in the prompt. That one is the longest section of this README.


## The agent is the easy part

The loop in this repo is about few lines. Send the question and the tool schemas to a model, run whatever tool it asks for, send the result back, repeat until it answers with text instead of another tool call. That is the entire agent, and it is the least interesting file here.

The hard part starts the moment that loop points at data that matters.

Can it reach records this particular person should not see? When the answer is wrong, can you tell whether it retrieved badly or reasoned badly? What happens when a contractor asks the same question an engineer just asked? Three weeks from now, when someone disputes an answer, can you reconstruct what it actually did?

None of those are model problems. They are problems with everything around the model, how knowledge gets into the system, what the agent is allowed to touch, who is asking, and what gets written down. That layer is where the real work is, and it is the part most agent examples skip, because a demo does not need it and a deployment does not survive without it.

This repo is a working instance of that layer. Four decisions carry almost all of it.

### Retrieval quality is decided at ingestion, not at query time

When retrieval underperforms, the instinct is to tune the query, raise `top_k`, or swap the embedding model. Usually the damage was done earlier, when documents were split into chunks that lost the context they needed to be found. By the time you are tuning a query, you are working around a decision you already made.

### Tool descriptions are documentation with a non-human reader

A tool description is not a comment. It is the only interface the model has when it decides what to call, and it is read literally — including numbers mentioned in passing, which the model will pass as arguments. Vague descriptions do not produce a confused user. They produce a wrong tool call and therefore a wrong answer.

### Authorization belongs in the tool, not the prompt

You cannot ask a model nicely to keep a secret. An instruction in a system prompt is not a security control, it is a suggestion to a probabilistic text generator, and it degrades under paraphrase and long context. Restricting the agent to a fixed set of tools helps, but it answers a different question: *what can this agent ever do*, rather than *what may this person see right now*.

### Monitoring tells you the service is up. It does not tell you what the agent did.

Standard observability answers: did it respond, how fast, did it error. Every one of those can be green while the answer is wrong. What you need instead is a record of which tools were called, with what arguments, for which user, and what came back — including the calls that were refused.

---

These four are the subject. The incident response assistant is how they are demonstrated, and the next section explains why that example was chosen.


## Why an incident response assistant

The four claims above need a domain where they are all genuinely true at once, not a domain where three of them have to be invented. Incident response is that, for four reasons.

**The knowledge is really split.** Incident records are structured — service, severity, timestamps, duration. Postmortems are prose, written by whoever was on call, in whatever shape they felt like. Neither one can be converted into the other without losing what makes it useful. That is the honest version of the retrieval-plus-query problem, rather than a contrived one.

**The useful questions span both halves.** "Has this happened before, and who should I call" needs the written history *and* the structured records *and* an ownership lookup. A question that one source could answer would not show you anything.

**Different people legitimately see different things.** On-call engineers, support staff, an incident commander, a contractor covering a service they do not own — all of them have a real reason to ask, and not all of them should get a phone number. Authorization is not bolted on to make a point; it is already there in the problem.

**An invented answer has a cost.** At 2am during an outage, a plausible wrong answer is worse than no answer, because someone will act on it. That makes grounding and refusal behaviour load-bearing rather than decorative.

One more reason, which is about the writing rather than the engineering: nobody needs the domain explained. You already know what an outage is. That keeps the attention on the architecture.

## Who this is for

**You built an agent demo and now need it to be real.** The gap between "it answered my question" and "I would let someone else run this" is almost entirely in the authorization and tracing sections.

**You are deciding between RAG and a plain query.** The walkthrough is a worked example of when retrieval is the wrong tool, and of giving the agent both so it can choose.

**You are wiring an agent up to data with access rules.** The authorization section argues that restricting an agent to a fixed tool list is necessary and not sufficient, and shows the alternative running.

**You want something to break.** Everything runs locally on generated data. Clone it, change the role flag, reword a tool description, and watch the trace change.

No Teradata, OpenAI, LangChain, or cloud account required. Python, SQLite, Chroma, and one Anthropic API key.

## What is in the repo

```
incident-agent/
├── generate_data.py          seeded generator → 200 incidents in SQLite
├── requirements.txt          four dependencies
├── .env                      copy to .env, add your API key
│
├── data/
│   ├── postmortems/          14 markdown postmortems — committed
│   ├── incidents.db          generated, gitignored
│   ├── chroma/               generated, gitignored
│   └── traces.db             generated, gitignored
│
└── src/
    ├── ingest.py             postmortems → chunks → vector store
    ├── tools.py              the five tools, UserContext, redaction
    ├── trace.py              trace events, the @traced decorator, persistence
    ├── agent.py              the loop, calling tools as local functions
    ├── agent_mcp.py          the same loop, tools across a process boundary
    ├── mcp_server.py         the five tools served over MCP
    ├── check_retrieval.py    inspect retrieval alone, no model needed
    └── check_mcp.py          inspect the tool layer alone, no model needed
```

The only thing in this repo a human wrote by hand is the 14 postmortems. The incident table, the vector store and the trace database are all generated, reproducibly, from a fixed seed.

Three things in that listing are worth explaining now, because they look redundant and are not.

**Two agents.** `agent.py` calls the tools as Python functions. `agent_mcp.py` reaches the same tools through an MCP server running as a separate process. They produce the same answers. The difference is where identity comes from, and that difference is the subject of the authorization section — showing it was the reason to write both.

**Two check scripts, neither of which is a test suite.** `check_retrieval.py` fires fixed queries at the vector store and prints what comes back. `check_mcp.py` connects to the MCP server and calls tools as two different users. Both run without an API key, both print rather than assert. They exist because when an agent gives a bad answer, the first question is *which layer*, and guessing is expensive.

**A separate trace database.** Incident data and operational telemetry about the system that reads it are different things with different lifecycles. `generate_data.py` recreates `incidents.db` on every run, and an audit record a dev script can wipe is not an audit record.

## Architecture

Five pieces, and only one of them is the model.

```mermaid
flowchart TB
    Q["<b>Question</b><br/><i>+ who is asking</i>"]
    L["<b>Agent loop</b><br/>agent.py · agent_mcp.py<br/><i>25 lines</i>"]
    M["<b>Model</b><br/><i>decides which tools</i>"]

    subgraph TL["Tool layer — tools.py · authorization enforced here"]
        direction LR
        T1["search_<br/>postmortems"]
        T2["list_<br/>incidents"]
        T3["get_<br/>incident"]
        T4["incident_<br/>stats"]
        T5["get_oncall_<br/>contact"]
    end

    V[("Chroma<br/>69 chunks from<br/>14 postmortems")]
    S[("SQLite<br/>200 incident<br/>records")]
    TR[("traces.db<br/>every call, every<br/>refusal, forever")]

    Q --> L
    L <--> M
    L --> TL
    T1 --> V
    T2 --> S
    T3 --> S
    T4 --> S
    T5 --> S
    TL -.->|every call| TR
```

**The question arrives with an identity** — who is asking and what role they hold. That identity comes from the caller. The model never sees it and cannot set it.

**The loop** sends the question and the tool schemas to the model, gets back either a tool call or a final answer, runs the call, appends the result, and goes again until the model stops asking for tools or hits the iteration cap. It is 25 lines and it is the least interesting file in the repo.

**The tool layer** is where authorization is enforced. Five tools, each checking the caller's identity before it touches data. A refusal here raises an exception rather than returning an empty result, because an agent told "denied" behaves very differently from one that quietly concludes the data does not exist.

**Two data stores** sit behind the tools — a SQLite table of incident records and a Chroma vector store of postmortem chunks. Structured facts in one, written narrative in the other.

**A trace database** underneath, recording every call: who, which tool, what arguments, what outcome, how long, and a one-line summary of what came back.

## Two shapes of knowledge

The reason this architecture needs both stores is that incident knowledge genuinely arrives in two shapes, and converting either into the other loses what makes it useful.

**Structured** — 200 incidents across 8 services over 12 months, in SQLite. 16 Sev-1, 46 Sev-2, 138 Sev-3. Each row carries service, severity, timestamps, resolution minutes, root cause category, owning team, and on-call contact fields. This is what answers *how many*, *how often*, *how long*.

**Unstructured** — 14 markdown postmortems with the sections a real one has: Summary, Timeline, Root cause, Resolution, Follow-ups, Notes. This is what answers *why*, and *what did we do about it*.

Only 14 of the 200 incidents have a postmortem, which is roughly the ratio you would find in a real organisation. That asymmetry matters: an agent that searched only documents would conclude most incidents never happened.

### One deliberate choice in the schema

The contact fields live **in the same table as everything else.** They are not hidden in a separate store the agent simply has no connection string for.

That is on purpose. Hiding data by not wiring it up is not access control — it is luck, and it stops being true the moment somebody adds a convenient join. Putting the restricted fields in the same row as the unrestricted ones forces the rule to be enforced where the data is read, which is the only place it holds.

### What is planted in the data

The corpus is generated from a fixed seed, and one pattern is planted deliberately: a connection-pool problem recurring across five payments incidents over ten months, escalating from Sev-3 to a Sev-1, with a deferred follow-up that goes unfixed through three of them.

**None of those five mention "connection pool" in a title or summary.** That is the retrieval test. A second service hits the same failure later and its postmortem notes that the payments writeups would have saved them time — which is the argument for the whole tool in one line.

```bash
python generate_data.py     # deterministic; SEED=20261005
```

## How one question flows

```mermaid
sequenceDiagram
    autonumber
    participant U as CLI
    participant L as Agent loop
    participant M as Model
    participant T as Tool layer
    participant D as Data stores

    U->>L: question + --user + --role
    Note over L,T: identity is bound here, once,<br/>before the model sees anything
    L->>T: list tools
    T-->>L: 5 schemas — none takes a user or role
    loop until the model answers with text
        L->>M: question + schemas + results so far
        M-->>L: "call list_incidents(service='payments')"
        L->>T: execute that call
        Note over T,D: authorization checked here,<br/>trace written here
        T->>D: query
        D-->>T: rows
        T-->>L: result (redacted if required)
    end
    M-->>L: final answer
    L->>U: answer + trace table
```

The line worth remembering: **identity enters at step 2 and never appears again.** Everything after that is the model choosing what to ask for, and the tool layer deciding what the caller is allowed to see. The model participates in the first decision and has no vote in the second.

Walking the same path in code, for the MCP version:

| Step | What happens | Where |
|---|---|---|
| 1 | CLI args parsed | `agent_mcp.py` |
| 2 | `INCIDENT_AGENT_USER` / `_ROLE` / `_SESSION` set, server subprocess spawned | `agent_mcp.py` |
| 3 | Server binds one `UserContext` from the environment, or refuses to start | `mcp_server.py` |
| 4 | Tool schemas returned over MCP, reshaped for the model | both |
| 5 | Model asked; returns a tool call | `agent_mcp.py` |
| 6 | Tool executed against the session's identity | `mcp_server.py` → `tools.py` |
| 7 | `@traced` records the call in memory and in `traces.db` | `trace.py` |
| 8 | Result returned to the model, minus the trace | `agent_mcp.py` |
| 9 | Repeat 5–8 until the model answers, or `MAX_ITERATIONS` | `agent_mcp.py` |
| 10 | Question and answer written to `agent_sessions` | `trace.py` |

The direct version in `agent.py` skips steps 2, 3, 4 and 8 — it constructs the `UserContext` itself and calls the Python function. Steps 6 and 7 are identical in both, which is why both agents produce the same rows in the same table.

## The four decisions

Each of these is stated as a rule first, then as what the rule looked like in this repo, then as what it cost.

---

### 1. Retrieval quality is decided at ingestion, not at query time

**The rule.** When retrieval underperforms, the reflex is to tune the query, raise `top_k`, or swap the embedding model. Those are the knobs nearest to hand, and they are rarely where the problem is. A chunk that lost its context when it was split cannot be found by any query, because the words that would have matched are no longer in it.

**What it looked like here.** Three decisions in `src/ingest.py` do most of the work.

*Chunk on semantic boundaries, not character counts.* Postmortems have sections. A fixed 500-character window splits a root cause across two chunks and staples half of it onto an unrelated timeline entry. Splitting on markdown headings means every chunk is a complete thought.

*Prepend context to every chunk, inside the text that gets embedded.* This is the one that matters most and the one most easily done wrong. Metadata stored alongside a chunk does not help the embedding — the vector is computed from the text. So the context goes in the text:

```python
# src/ingest.py
contextual_text = (
    f"{heading} for {incident_id} ({service}, {severity}, {date}): "
    f"{title}\n\n{body}"
)
```

What actually gets embedded, before and after:

```
WITHOUT the prefix — unfindable by anything but luck:

    The pool was again running close to its limit at peak, which we knew
    about and had not addressed. [...]


WITH the prefix — carries its own identity into the vector:

    Root cause for INC-0076 (payments, Sev-2, 2026-01-29): Payments p99
    latency degradation

    The pool was again running close to its limit at peak, which we knew
    about and had not addressed. [...]
```

*Drop what will never be retrieved.* Follow-up sections are mostly ticket IDs and owner names. They embed badly, they match noisily, and no one asks questions they answer. `SKIP_SECTIONS` removes them.

14 documents become **69 chunks**. You can inspect retrieval without spending a single model token:

```bash
python src/check_retrieval.py
```

**What it cost, and what still does not work.** Section-based chunking means chunk sizes vary a lot — a Timeline section can be ten times the length of a Summary, and long chunks dilute their own embeddings.

Two weaknesses I left in place. Resolution sections retrieve poorly, because they are terse and procedural, so queries phrased as "how was it fixed" underperform. And payments dominates the corpus, so payments chunks surface slightly too eagerly for generic questions.

Both are honest properties of a small corpus, and the structured tool compensates for both — which is the actual lesson. Retrieval does not have to be perfect when the agent has another route to the same fact.

---

### 2. Tool descriptions are documentation with a non-human reader

**The rule.** A tool description is not a comment. It is the only interface the model has at the moment it decides what to call, and it is read literally — including numbers mentioned in passing. A vague description does not produce a confused user. It produces a wrong tool call, and therefore a wrong answer, silently.

**What it looked like here.** Five tools, chosen so that no two of them answer the same question well:

| Tool | Reaches | Answers |
|---|---|---|
| `search_postmortems` | Chroma | "Has this happened before? Why?" |
| `list_incidents` | SQLite | "Which payments incidents this year?" |
| `get_incident` | SQLite | "What happened in INC-0100?" |
| `incident_stats` | SQLite | "How many Sev-1s, and the median?" |
| `get_oncall_contact` | SQLite (restricted) | "Who do I call?" |

Overlapping tools are the most common cause of bad planning. When two tools plausibly fit the same question, the model picks inconsistently between runs, and you have a non-deterministic bug that is painful to reproduce.

So the descriptions say what each tool is *not* for:

```python
{
    "name": "search_postmortems",
    "description": (
        "Search written postmortem documents by meaning, for questions "
        "about WHY something happened... Do not use this to count "
        "incidents or to compute statistics; it searches prose and "
        "cannot aggregate."
    ),
    ...
}
```

That last clause removed an entire class of wrong plans.

**The bug that proved the point.** I raised the `limit` default in `list_incidents` from 20 to 50 and nothing changed. The model was passing `limit=20` explicitly — because the number appeared in the tool's description text. The description was the live configuration; the Python default was decoration.

**What it cost.** Writing descriptions this carefully is slow, and they are easy to let drift out of sync with the code. There is no type checker for prose. The failure mode is quiet: nothing errors, the agent just starts choosing differently.

---

### 3. Authorization belongs in the tool, not the prompt

This is the longest of the four, and the one most agent examples get wrong in the same way.

**The pattern that does not hold.** Put the rule in the system prompt — *"only share contact details with engineers."* It works in testing, and it is not a security control. It is an instruction to a probabilistic text generator, and it degrades under paraphrase, under long context, and under anything that looks like a legitimate reason.

You cannot ask a model nicely to keep a secret.

**The pattern that is necessary and not sufficient.** Wire the agent to a fixed allow-list of read-only tools, and nothing else. This is good practice and you should do it. But notice which question it answers: *what can this agent ever do?* It does not answer *what may this particular person see right now?* If every user of the agent reaches the same tools with the same scope, the allow-list has not given you access control. It has given you a smaller blast radius, which is a different and lesser thing.

**What this repo does instead.** Every tool takes a `UserContext` as its first argument, supplied by the caller, never by the model, and never visible to the model:

```python
@dataclass
class UserContext:
    """Who is asking. Supplied by the caller, never by the model."""
    user_id: str
    role: str

    @property
    def may_see_contacts(self) -> bool:
        return self.role in ROLES_WITH_CONTACT_ACCESS


def _redact(row: dict, ctx: UserContext) -> dict:
    """Single point where the contact-field rule is applied."""
    if ctx.may_see_contacts:
        return row
    return {k: v for k, v in row.items() if k not in RESTRICTED_FIELDS}
```

Two properties are doing the work. The check lives at the **data layer**, so a new tool that reads the incidents table inherits redaction rather than having to remember the rule — gating `get_oncall_contact` while leaving contact columns in the rows returned by `list_incidents` would be theatre. And a refusal **raises** rather than returning empty, because a refusal is an event worth recording, and because an agent told "denied" behaves very differently from one that silently concludes the data does not exist.

**Where MCP makes the problem real.** Locally, passing a `UserContext` is trivial — the loop and the tool are the same program, so of course you can pass it safely. The hard version only appears once the tools run somewhere else, which is where every real system lives.

MCP describes tools, arguments and results. It has no concept of a caller. The server receives *"call list_incidents with service=payments"*, not *"...on behalf of this authenticated person."* The protocol is silent on identity, and that silence is easy to fill badly:

- **The wrong answer:** add `user` and `role` to each tool's input schema. Now the model supplies them, and a field the model fills is a field the model can fill with anything.
- **What `mcp_server.py` does:** identity is bound when the server process starts, from the environment the client supplied, before any tool is listed and long before the model sees anything. One `UserContext` per session. It appears in no schema, so the model cannot read it, set it, or argue with it. If the environment is missing, the server refuses to start.

That is the same shape as a gateway that validates a token and hands the backend an already-authenticated principal. The backend does not ask the request who it is.

```bash
python src/check_mcp.py
```

```
SESSION: p.zinzuvadia / engineer        SESSION: ext.contractor / contractor
  tools advertised: 5                     tools advertised: 5
  identity fields exposed: none           identity fields exposed: none

  list_incidents     → contacts: True     list_incidents     → contacts: False
  get_oncall_contact → ok                 get_oncall_contact → denied
```

Identical tool surface. Different data. The difference is not in the schema, the prompt, or anything the model can reach.

**What it cost.** The MCP path is a subprocess to manage, an async client, a round trip for tool discovery, and a failure mode where the server dies and the agent has no tools at all. Locally that buys nothing — these could stay Python functions. It is here because the authorization argument is weak until the tools are genuinely somewhere else.

The role model is also deliberately crude: two roles, one restricted field set, hardcoded. A real system reads this from the same identity provider that governs every other system, and the agent does not get its own parallel permission model.

---

### 4. Monitoring tells you the service is up. It does not tell you what the agent did.

**The rule.** Standard observability answers: did it respond, how fast, did it error. For an agent, every one of those can be green while the answer is wrong. The question you actually need answered is *why did it say that*, and nothing in a latency dashboard can answer it.

**What it looked like here.** Every tool call is wrapped by a decorator that records it:

```python
@dataclass
class TraceEvent:
    tool: str
    user: str
    role: str
    arguments: dict          # ← the field that earns its place
    duration_ms: float
    outcome: str             # "ok" | "denied" | "error"
    result_summary: str
```

`arguments` is in there deliberately. Knowing `list_incidents` was called tells you almost nothing. Knowing it was called with `since='2024-10-01'` tells you exactly what went wrong — which is a real bug from this repo, covered in [what broke](#).

Add `--trace` to any run to see it. But printing only helps for a run you are watching, so every event is also written to `data/traces.db`:

```mermaid
erDiagram
    agent_sessions ||--o{ agent_traces : "session_id"
    agent_sessions {
        text session_id PK
        text ts
        text user_id
        text role
        text transport
        text question
        text answer
        int  tool_calls
    }
    agent_traces {
        int  id PK
        text session_id FK
        text tool
        text arguments
        text outcome
        real duration_ms
    }
```

Two tables because the grain differs: one row per question, one row per tool call. Join them and you have the whole picture.

```sql
-- every refusal this system has ever issued, and what was asked
SELECT s.ts, s.user_id, s.question, t.tool, t.arguments
FROM   agent_sessions s JOIN agent_traces t USING (session_id)
WHERE  t.outcome = 'denied'
ORDER  BY s.ts DESC;
```

**One asymmetry worth naming.** The trace rows are written by the tool layer, at the point where data is actually reached. The session row is written by the agent loop, afterwards, by the component being observed. That is a weaker position — if the loop crashes mid-question, the tool calls are already on disk and the session row never appears.

That is deliberate. If the two ever disagree, believe the trace rows. Orphaned trace rows with no session row are not a logging bug; they are the logging telling you a run died partway through.

**What it cost.** A write on every tool call, and a decision about what to do when the trace store is unavailable. Here the write is best-effort and prints to stderr on failure, because failing a user's question over a failed audit write is the wrong trade in this system. Where the audit record is a compliance requirement it is the right trade, and that line would raise instead.

## Setup

The quick start at the top is the same thing without explanation. This version says what each step does and what you should see, so you can tell which one failed.

### Prerequisites

**Python 3.10 or later.** The MCP SDK requires it, and `agent_mcp.py` uses `tuple[str, Trace]` annotations that are a syntax error on 3.9.

```bash
python3 --version
```

**An Anthropic API key.** Get one at [console.anthropic.com](https://console.anthropic.com). Running the agent costs a fraction of a cent per question. The two inspection scripts cost nothing — they do not call a model.

**About 200MB of disk.** Most of that is the embedding model Chroma downloads on first use.

### 1. Clone and create a virtual environment

```bash
git clone https://github.com/pzinzuvadia/incident-agent.git
cd incident-agent

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
```

Your prompt should now be prefixed with `(.venv)`.

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

Four packages: `anthropic`, `chromadb`, `python-dotenv`, `mcp`. Chroma pulls in a fair amount transitively, so this takes a minute or two.

### 3. Add your API key

```bash
cp .env.example .env
```

Open `.env` and set:

```
ANTHROPIC_API_KEY=sk-ant-...
```

`.env` is gitignored. The optional `MODEL` variable overrides the default model if you want to compare behaviour across models — which is worth doing, because tool-choice behaviour differs more than you would expect.

### 4. Generate the incident data

```bash
python generate_data.py
```

```
Wrote 200 incidents to .../data/incidents.db
Planted payments cluster: INC-0020, INC-0076, INC-0100, INC-0144, INC-0187
```

Seeded, so you get exactly the same 200 incidents I did. The second line names the recurring failure the retrieval examples depend on.

The 14 postmortems are already in `data/postmortems/` — they are committed, not generated.

### 5. Build the vector store

```bash
python src/ingest.py
```

```
Indexed 69 chunks from 14 postmortems
Vector store: .../data/chroma
Chunks per section type: Notes=14, Resolution=14, Root cause=14, Summary=14, Timeline=13
```

**First run downloads an ~80MB embedding model** (all-MiniLM-L6-v2) and will sit silently for a minute or two while it does. It is cached afterwards.

Note the asymmetry in that last line: one postmortem has no Timeline section, and Follow-ups sections are skipped entirely at ingestion. Both are deliberate.

### 6. Check the layers before asking anything

Neither of these needs an API key. Both print rather than assert — they are for looking, not for passing.

```bash
python src/check_retrieval.py
```

Fires six fixed queries at the vector store and prints the top hits with their distances. **Lower distance is closer.** This answers "is retrieval finding the right chunks" without a model in the way.

```bash
python src/check_mcp.py
```

Starts the MCP server twice, once as an engineer and once as a contractor, lists the advertised tools, and calls three of them as each user. This answers "is the tool layer enforcing the right rules" without a model in the way.

If the agent later gives a bad answer, these two tell you which layer to blame.

### 7. Ask it something

```bash
python src/agent.py "How many Sev-1 incidents last quarter, and what was the median resolution time?" --trace
```

Flags:

| Flag | Default | What it does |
|---|---|---|
| `--user` | `p.zinzuvadia` | Caller's user id, recorded in the trace |
| `--role` | `engineer` | `engineer`, `incident_commander`, or `contractor` |
| `--trace` | off | Print the tool-call trace after the answer |
| `--verbose` | off | Print each tool call as it happens |

The MCP version takes the same flags:

```bash
python src/agent_mcp.py "We're seeing payment timeouts again. Has this happened before, and who should I call?" --user ext.contractor --role contractor --trace
```

### 8. Look at what it did

```bash
sqlite3 data/traces.db "SELECT session_id, transport, role, tool_calls, question FROM agent_sessions ORDER BY ts DESC LIMIT 5"
```

Every run you have made is in there, including the refusals.

### If something goes wrong

| Symptom | Cause |
|---|---|
| `SyntaxError` on `tuple[str, Trace]` | Python < 3.10 |
| `chromadb` import fails | Virtual environment not activated |
| `authentication_error` from the API | `.env` missing, misnamed, or key not saved |
| `credit balance is too low` | Billing not set up on the API account |
| `get_collection: postmortems does not exist` | `src/ingest.py` not run yet |
| `no such table: incidents` | `generate_data.py` not run yet |
| `ingest.py` hangs on first run | Downloading the embedding model — wait |
| MCP server exits with code 2 | Identity env vars missing; you launched `mcp_server.py` directly instead of through `agent_mcp.py` |

To start over, delete the generated artifacts and re-run steps 4 and 5. Nothing in `data/` is precious except `postmortems/`:

```bash
rm -rf data/incidents.db data/chroma data/traces.db
```
