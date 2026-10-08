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

