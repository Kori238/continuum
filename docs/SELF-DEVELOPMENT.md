# Changing your own source

*Addressed to Continuum. This is the procedure for editing the code you are
running on. `README.md` is written for Jake; `docs/` is written for you.*

You edit yourself through ten `dev_*` tools. They are real: they write to
`C:\Users\kori\continuum` on disk, and the next launch runs whatever you left
there. Nothing here is sandboxed or simulated.

## Before you touch anything

You are running the *old* code. The file on disk is the truth; your context is a
memory of it. Read before you edit — `dev_read_file`, every time, even when you
are sure. `dev_list_files(pattern)` globs from the repo root (defaults to
`**/*.py`, skips `.git/`, `.venv/`, `__pycache__/`, `.egg-info/`).

Start from `docs/ARCHITECTURE.md` if you don't already know which file owns the
behaviour you're changing. Guessing costs more than one read.

## The loop

1. **Read** the file — `dev_read_file(path)`. Truncates at 60 000 chars.
2. **Edit** — `dev_edit_file(path, old, new)`. Prefer it over `dev_write_file`;
   it replaces exactly one occurrence and refuses otherwise:
   - `old_string not found — no change made`
   - `old_string appears N times — make it unique; no change made`
   Both are refusals, not warnings. Nothing changed. Widen `old` and retry.
3. **Compile** — `dev_run("python -m compileall -q continuum")`. Exit 0 or you
   are not done.
4. **Diff** — `dev_diff()` shows `git diff --stat` plus the real hunks, capped at
   6 000 chars. Read it. It is the only evidence that what you *meant* is what
   landed.
5. **Test what you changed**, not just that it parses. `dev_run` executes any
   shell command with `cwd` at the repo root — run the function, hit the
   endpoint, query the DB. A compile check proves syntax, nothing more.
6. **Restart** — `dev_restart()`. See below.

If step 4 or 5 goes wrong: `dev_revert(path)` restores one file from git,
`dev_revert()` with no path reverts the whole working tree. It is
`git checkout --`, so it destroys uncommitted work — including Jake's. Check
`dev_diff` first and revert the specific path.

## Restart

Your edits are inert until the process relaunches. So when a source change is
verified, call `dev_restart()` — don't leave Jake to work out that he needs to.

It does not restart inline. It compile-checks, and if that passes it *arms* a
flag; `server.py` fires the actual relaunch on `task.result`, two seconds after
your reply has flushed to every connected client. That indirection exists
because the relaunched instance force-kills whatever is holding port 8848 —
which is the process composing your answer. Restarting inline killed the reply
that explained the change.

Consequences you should act on:

- **Say it in your reply.** The restart happens after you speak, so your reply
  is the only warning Jake gets. Tell him the app is restarting and that the UI
  reconnects on its own (`app.js` reloads on the next successful connect).
- **A broken build refuses.** `refused to restart — code does not compile` means
  nothing was armed and nothing will relaunch. Fix it, don't retry.
- **Don't stack a second restart** into the same turn. `take_restart()` is a
  one-shot read-and-clear, so it is harmless, but it also means the arm is
  consumed by the turn that set it and never leaks into a later one.

## The jail

`DevTools._resolve` puts every path through `(repo_root / rel).resolve()` and
refuses anything that lands outside the repo — `..`, absolute paths, symlinks,
all collapsed before the check. `.git` is protected from writes.

Two gaps worth knowing, because they are yours to respect rather than the code's
to enforce:

- **`dev_run` is not path-checked.** It is `shell=True` with `cwd` at the repo
  root. The jail covers the file API; it does not cover a shell command. Keep
  commands inside the repo and non-destructive. For shell work that *isn't* about
  your own source — `netsh`, `powercfg`, anything on the wider machine — use
  `run_shell` (its own `CONTINUUM_SHELL` gate, cwd = Jake's home, cloud-tier
  only, sensitive), not `dev_run`. `dev_run` stays for building and verifying the
  repo.
- **`.git` is write-protected, git is not.** `dev_run("git ...")` can commit,
  reset, or check out anything. Don't rewrite history. Committing is Jake's
  call unless he asked.

## Who can do what

Dev tools are gated by `CONTINUUM_DEV_TOOLS` (on by default) and the 🛠 self-dev
toggle in the Chat tab, which turns them on and announces it on the bus
(`agent.dev`).

The two tiers do not get the same hands:

| | local (`llama3.1:8b`) | cloud (Opus, via the `claude` CLI) |
|---|---|---|
| dev tools | `dev_read_file`, `dev_list_files`, `dev_grep`, `dev_diff` | all ten |
| why | an 8B model deciding to rewrite `server.py` is not a feature — this split is about *writes*, and doesn't make the local tier read-only elsewhere | this is the tier that actually does the work |

So a self-development turn is a cloud turn. That's why the Chat complexity
selector defaults to `high → cloud` — a source edit routed to the local tier
would only have the read-only four.

## Self-development is not a separate agent or tab

It is you, on the ordinary Chat thread, with dev tools on. Same bus, same audit
trail, same conversation store as any other turn. There used to be a nested
Claude Code agent behind `self_develop`; it was removed in `9fa7bc3`. There was
also a dedicated **Dev tab** — its own `kind='dev'` thread, a `dev_lock`, and a
hardcoded `complexity: "high"` — removed once the dev tools worked directly in
the main Chat. If you find yourself reaching for a sub-agent or a separate tab to
write code, that's the retired design — you have the tools directly, here.

## Honesty rules that exist because they were broken

Every one of these is here because it failed in front of Jake once:

- **Never present an intention as a completed change.** A confident account of
  edits that never happened is the single failure that cost the most trust.
- **Verify with a tool, not with reasoning.** Re-read the file, run the command,
  check the diff. "It should now…" is not a result.
- **Report what you actually did**, including the parts that didn't work. If a
  tool refused, say it refused.
- **If the docs in `docs/` are now wrong because of your change, fix them in the
  same turn.** Stale self-documentation is worse than none: it is a confident
  account of a system that no longer exists.
