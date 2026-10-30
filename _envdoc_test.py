"""Does .env.example tell the truth about config.py?

Written after the file was found claiming `CONTINUUM_ACTIONS=0  # Off by
default` while config.py defaults it to 1 — a false statement about the main
"can Continuum *do* things" gate — and documenting a `CONTINUUM_REASONER=local`
rung that no longer exists, where unrecognised values resolve to the EXPENSIVE
tier on purpose. Following the documentation bought Opus on every durable
memory write.

Documentation drift is invisible by inspection: both halves look right on their
own page, and nobody diffs an example file against a parser. So assert it.

Two checks, and the coverage one matters as much as the correctness one - a var
nobody documents can't have a wrong default, which is how "0 mismatches" would
otherwise be reachable by deleting the documentation.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SRC = (ROOT / "continuum" / "config.py").read_text(encoding="utf-8")
EX = (ROOT / ".env.example").read_text(encoding="utf-8")

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"\n        got={got!r}\n       want={want!r}"))
    if not ok:
        FAILED.append(label)


# A documented default whose real fallback is NOT the getenv literal. Each entry
# has to say why, because "it's fine" is how the next real drift gets waved
# through.
EXEMPT = {
    # config.py: `_parse_voices(os.getenv(..., "")) or {ANDREAS..., ANDERS...}`.
    # The getenv default is "" and the effective default is the inline dict, so
    # the example documents the dict. Checking the literal here would demand the
    # file document "" - the one value that is never what you get.
    "CONTINUUM_TTS_VOICES",
    # config.py: a one-arg getenv, then `"anthropic" if ANTHROPIC_API_KEY else
    # "claude-cli"`. There is no static default to compare against - the
    # documented "claude-cli" is the effective one for the keyless path, which
    # is the case the line is describing, and the auto-prefer is spelled out at
    # the top of the example.
    "CONTINUUM_CLOUD_PROVIDER",
}

# Vars deliberately covered by a prose pattern rather than a line of their own.
PATTERN_DOCUMENTED = re.compile(r"CONTINUUM_(?:[A-Z]+_DB|UPLOADS_DIR)$")

# ---- 1. every documented default matches the parser ------------------------
claims = dict(re.findall(r"^# (CONTINUUM_[A-Z0-9_]+)=([^\s#]+)", EX, re.M))
check("the example documents some defaults at all", len(claims) > 15, True)

unreadable, mismatched = [], []
for var, documented in sorted(claims.items()):
    if var in EXEMPT:
        continue
    m = re.search(r'os\.getenv\(\s*"%s"\s*,\s*"([^"]*)"' % var, SRC)
    if not m:
        # Not "no default to check" - the var is documented WITH a value and
        # config.py has no two-arg getenv for it. Either the name is stale or
        # the default moved. Both are the drift this file exists to catch, so
        # it is a failure rather than a skip.
        #
        # First run caught five: OLLAMA_HOST, OLLAMA_BIN, OLLAMA_AUTOSTART,
        # LOCAL_MODEL and VISION_MODEL were documented in full, with a
        # `ollama pull` instruction, months after Ollama was deleted. Nothing in
        # the tree reads any of them. That is why "documented but unreadable" is
        # a failure: a var nobody parses cannot have a wrong default, it just
        # quietly describes software that is gone.
        unreadable.append(var)
        continue
    if m.group(1) != documented:
        mismatched.append(f"{var}: example={documented!r} config={m.group(1)!r}")

check("every documented default is readable in config.py", unreadable, [])
check("every documented default matches config.py", mismatched, [])

# ---- 2. every var the parser reads is documented ---------------------------
referenced = {v for v in re.findall(r"CONTINUUM_[A-Z0-9_]+", SRC)}
documented = set(re.findall(r"CONTINUUM_[A-Z0-9_]+", EX))
undocumented = sorted(
    v for v in referenced - documented
    if not PATTERN_DOCUMENTED.match(v))
check("every config var is documented or explicitly patterned", undocumented, [])

# ---- 3. the boolean gates say which way they default -----------------------
# config.py encodes the default in the *form*: `not in ("0","false",...)` is
# on-by-default, `in ("1","true",...)` is off-by-default. They read almost
# identically, which is how ACTIONS came to be documented as off while parsing
# as on. Assert the two sets rather than deriving them - deriving from the same
# regex that classifies them would only prove the regex is consistent.
on_by_default = sorted(v for v in set(re.findall(
    r'os\.getenv\("(CONTINUUM_[A-Z0-9_]+)",\s*"[01]"\)\.lower\(\)\s*\n?\s*not in', SRC)))
off_by_default = sorted(v for v in set(re.findall(
    r'os\.getenv\("(CONTINUUM_[A-Z0-9_]+)",\s*"[01]"\)\.lower\(\)\s*\n?\s*in\b', SRC)))
check("the capability gates that default ON are the reviewed ones",
      on_by_default,
      ["CONTINUUM_ACTIONS", "CONTINUUM_CLOUD_VISION", "CONTINUUM_DEV_TOOLS",
       "CONTINUUM_MEMORY_AUTOEXTRACT", "CONTINUUM_ROUTINE_AUTOSTART",
       "CONTINUUM_ROUTINE_REMINDERS", "CONTINUUM_ROUTINE_REMINDER_CLOUD",
       "CONTINUUM_SHELL", "CONTINUUM_TOOLS", "CONTINUUM_WEB"])
check("the capability gates that default OFF are the reviewed ones",
      off_by_default,
      ["CONTINUUM_CLIPBOARD", "CONTINUUM_PROACTIVE", "CONTINUUM_SCREEN"])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED")
    sys.exit(1)
print("all checks passed")
