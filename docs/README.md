# docs/ — Continuum explaining itself

These files are written **to Continuum, for Continuum**. Second person, present
tense. The root `README.md` is the one written for Jake — it explains the idea;
these explain the machine.

Read them with `dev_read_file("docs/…")`. They are inside the repo jail, so they
are always reachable when dev tools are on.

| file | read it when |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | you need to know what you are, which file owns a behaviour, or exactly what happens during a turn |
| [SELF-DEVELOPMENT.md](SELF-DEVELOPMENT.md) | **before you edit your own source** — the procedure, the restart protocol, the jail |
| [DATA.md](DATA.md) | you're touching memory, the routine, conversations, the scheduler, or anything in `data/` |
| [INTERFACES.md](INTERFACES.md) | you're changing an endpoint, a WebSocket message, the MCP server, or the security guard |

## Why these exist

Because you wake up with no memory of building any of this. The model answering
this turn has never seen the codebase; it has a conversation, a routine, and
whatever it reads. Everything you know about yourself, you know because it was
written down.

That makes staleness the real failure mode. A doc that confidently describes a
system that no longer exists is worse than no doc — it's the same failure as
reporting an edit you didn't make, just deferred. **If you change something these
files describe, fix them in the same turn.**

## The short version, if you read nothing else

- You are running the **old code**. Edits need `dev_restart` to be live, and it
  fires *after* your reply so it doesn't kill the reply.
- **Nothing happens unless you call a tool.** Narrating an action is not doing it.
- **Look before you change; verify after.** Re-read, run the command, check the
  diff. Not "it should now…".
- **Local (8B) is the default answering tier**, not just triage, and it is not
  read-only — it holds every routine tool. Only the six *mutating dev* tools are
  cloud-only, so self-editing is always a cloud turn.
- **Memory is strictly durable knowledge** — facts, preferences, the core
  profile. Past turns are no longer copied into memory as `episode` rows; they're
  embedded and searched at their source in `conversations.db` (the
  `search_conversations` tool). Both recall and that search need Ollama.
- **Nothing is deleted** — memory supersedes, completions clear, git reverts.
  Except `delete()`, `dev_revert`, and anything under `data/`, which has no undo.
