# AI voice agent & AI receptionist tools — Vapi, Twilio, Retell

![Tests](https://img.shields.io/badge/tests-passing-2ea44f)
![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-3776ab)
![Vapi](https://img.shields.io/badge/voice-Vapi%20%7C%20Twilio%20%7C%20Retell-6d28d9)

Production-grade external-tool integrations for an AI voice receptionist on
**Vapi**, **Twilio**, or **Retell** — the engineering that decides whether calls
get **resolved or dropped**.

Built by [Vroom Analytics](https://vroomanalytics.com) as public proof for our
**AI voice-receptionist service** — *"Never miss another call."*

---

![Voice receptionist tools demo](assets/demo.gif)

## The problem

Voice assistants talk fine. Give a modern voice model a phone line and it will
greet callers, sound natural, and hold a conversation. That was never the hard part.

The hard part is **tool wiring** — the moment the assistant has to *do* something:
check the calendar, look up the caller, book the appointment, hand off to a human.
That is where production voice deployments stall:

- The booking API is slow, so the assistant fills the silence by **inventing** an opening.
- The slot the caller wanted was just taken, and the assistant **confirms it anyway**.
- The CRM lookup fails, so the assistant **guesses** the caller's name.
- Nothing works, and instead of escalating, the assistant **loops politely** until the caller hangs up.

Every one of those is a dropped call wearing a friendly voice. This repo shows the
pattern we use on every voice build to prevent them — one hard rule, enforced in code:

> **The assistant never guesses.** A failed tool call degrades to a fallback or
> escalates to a human. It never invents names, times, or bookings.

## Architecture

```
                    +----------------------+
                    |  Voice platform      |
                    | (Vapi/Twilio/Retell) |
                    +----------+-----------+
                               | function calls
                               v
                    +----------------------+
                    |     ToolRuntime      |  <-- the hard rule lives here
                    |  timeout + retry +   |
                    |  fallback / handoff  |
                    +--+------+------+-----+
                       |      |      |     |
              +--------+ +----+--+ +-+---+ +---------+
              | booking| |  CRM  | | human | | validation |
              |adapter | |adapter| |handoff| | (no backend|
              +---+----+ +---+---+ +---+---+ |  call on   |
                  |          |         |     | bad input) |
        +---------+----------+---------+-----+------------+
        |  ADAPTER INTERFACES (adapters/base.py)           |
        |  SimulatedBookingAdapter (in-memory, this repo) |
        |  YOUR PRODUCTION ADAPTERS plug in here          |
        +--------------------------------------------------+
```

Three integrations ship as examples:

| Tool | What it does | On failure |
|---|---|---|
| `check_availability` | Free slots for a date | Honest fallback: takes preferred time for human confirmation |
| `book_appointment` | Books a slot; conflicts surface **real alternatives** | No degraded answer exists → **human handoff** with caller facts |
| `lookup_customer` | CRM lookup by caller phone | Stale cache (flagged, phrased as a question) → generic greeting. Never a guessed name. |
| `escalate_to_human` | Explicit escalation | Records full context; always gives the assistant something true to say |

Every external system sits behind an adapter interface (`adapters/base.py`).
The simulated adapters here use in-memory data — **no network, no API keys** —
and your production adapters (Acuity, HubSpot, Twilio SMS, …) plug into the
same interfaces. Credentials live in environment variables at the seam,
never in tool logic. Each interface documents its **PRODUCTION SEAM**.

## File tour

```
voice-receptionist-tools/
├── src/voice_tools/
│   ├── runtime.py            # ToolRuntime: timeout/retry/fallback/handoff + Vapi schemas
│   ├── retell.py             # Retell AI wiring: custom-function defs + webhook handler
│   └── adapters/
│       ├── base.py           # Adapter interfaces + domain exceptions + PRODUCTION SEAM notes
│       └── simulated.py      # In-memory adapters (latency/failure injection for tests)
├── tests/
│   ├── test_runtime.py       # timeout → fallback; retry recovery; unresolvable → handoff
│   ├── test_tools.py         # conflict → alternatives; validation; CRM degradation
│   └── test_retell.py        # Retell defs mirror runtime (no drift); webhook routing; never-guess on Retell path
├── requirements.txt          # pytest only -- the runtime itself is stdlib
├── LICENSE                   # MIT
└── README.md
```

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

Expected: `23 passed`. The suite covers the four failure shapes that sink
voice deployments in production:

1. **Tool timeout → fallback fires** (slow calendar degrades to a callback offer, never hangs the call)
2. **Bad input → graceful degradation** (garbage date/phone is validated before any backend call)
3. **Unresolvable request → human handoff, nothing guessed** (dead booking system pages a human with caller-supplied facts only — no fabricated confirmation)
4. **Booking conflict → offers alternatives** (taken slot surfaces real openings; the taken slot is never re-offered)

Plus: retry recovery on flaky backends, stale-cache CRM answers flagged and phrased
as questions, Vapi-compatible function schemas (`runtime.function_schemas()`)
so the tools drop into a Vapi assistant config unchanged — wire `dispatch()`
behind your function-call endpoint — and the Retell equivalent
(`retell.retell_function_definitions()` + `retell.handle_function_call()`)
so the same runtime drives Retell custom functions with zero tool-logic changes.

## Projected platform costs

Transparency is part of the service: a voice receptionist has ongoing platform
costs beyond the build. Figures below are **rough, illustrative estimates** —
verify against current Twilio / voice-provider pricing before quoting:

- **Telephony (Twilio):** on the order of ~$1–2/month per phone number, plus
  ~$0.01–0.02/minute for inbound call legs (two legs on some setups).
- **Voice AI (speech-to-text + LLM + text-to-speech):** roughly ~$0.05–0.15 per
  minute of conversation depending on models and provider.
- **Illustrative total:** a business taking ~400 minutes/month of receptionist
  calls lands somewhere around **~$30–70/month in platform costs** — the line
  item we show clients before they sign, not after.

Our managed-care plan bundles these under one monthly fee so the client never
thinks about per-minute metering after launch.

## Monthly peace of mind

Setup is just day one. The **$99/mo care plan** keeps this running —
monitoring, fixes, and monthly optimization, so you never think about it again.
It's the same plan behind every voice build we ship, and it's how a voice
receptionist stays a solved problem instead of becoming a second job.

See the full offer, packages, and the care plan here:
**https://vroomanalytics.com/voice-receptionist/**

## What not to automate

Honest boundaries, from real deployments:

- **Don't let the assistant confirm what the system didn't confirm.** A booking,
  payment, or order the backend didn't acknowledge must escalate — a confident
  "you're all set" for something that never happened is the most expensive
  failure a voice agent can produce.
- **Don't automate the upset caller.** Sentiment going south is the highest-ROI
  handoff trigger you have; a human saves the relationship, a bot reading a
  de-escalation script usually doesn't.
- **Don't wire tools without failure paths.** If a tool has no timeout, no
  retry, and no fallback, it isn't production-ready — it's a demo that hasn't
  met a bad Tuesday yet.
- **Don't personalize from stale data without saying so.** A cached CRM record
  is fine to use; presenting it as live fact is how you call someone by the
  wrong name.

## Companion links

- **Interactive demo** — watch the pattern run: https://vroomanalytics.com/voice-receptionist/#demo
- **Companion article** — *The part of a voice receptionist that actually matters isn't the voice*: https://vroomanalytics.com/blog/voice-receptionist-tools-matter/

## License

MIT — see [LICENSE](LICENSE). Use the pattern, steal the hard rule, tell us
what broke.
