"""Configuration — defaults, plus a tiny .env loader so keys stay out of code."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _detect_cloud_provider() -> str:
    """How to reach the cloud tier.

    Explicit override wins; otherwise prefer the metered API when a key is set,
    else fall back to the Claude Code CLI (reuses your subscription login).
    """
    override = os.getenv("CONTINUUM_CLOUD_PROVIDER")
    if override:
        return override.strip()
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "claude-cli"


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from .env into the environment (no dependency)."""
    env = _ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _parse_voices(raw: str) -> dict[str, str]:
    """Parse `label:voice_id,label:voice_id` into a map.

    Returns {} for blank or unparseable input so the caller falls back to the
    shipped defaults — a typo here should cost you the override, not all audio.
    """
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        label, sep, vid = part.partition(":")
        if sep and label.strip() and vid.strip():
            out[label.strip()] = vid.strip()
    return out


@dataclass(frozen=True)
class ModelTier:
    """One rung of the escalation ladder."""
    name: str
    provider: str  # "claude-cli" | "anthropic"
    model: str


@dataclass(frozen=True)
class Config:
    # The ladder is two rungs of the SAME provider. There is no local tier:
    # Ollama was removed in favour of sending every turn to Claude, so the only
    # axis left is how hard the question is, not where it is allowed to run.
    cloud_fast: ModelTier   # rung 1: small, frequent, non-user-facing work
    cloud: ModelTier        # rung 2: the heavy thinking
    claude_cli: str         # executable name/path for the Claude Code CLI
    embed_model: str        # the ONE fixed embedder, in-process — keeps memory cross-model
    memory_db: str          # path to the SQLite context store
    conversations_db: str   # path to the SQLite conversation history
    scheduler_db: str       # path to the SQLite scheduled jobs
    messages_db: str        # path to the SQLite incoming-message log
    routines_db: str        # path to the SQLite routines store
    workouts_db: str        # path to the SQLite Speediance workout history
    activity_db: str        # path to the SQLite attention/activity timeline
    nutrition_db: str       # path to the SQLite calorie/macro intake log
    body_db: str            # path to the SQLite body metrics store (weight/height/steps)
    health_db: str          # path to the SQLite recovery store (sleep/HRV/resting-HR/SpO2 → readiness)
    ledger_db: str          # path to the SQLite append-only action ledger (audit trail)
    flashcards_db: str      # path to the SQLite flashcard/spaced-repetition store
    budget_db: str          # path to the SQLite daily-allowance store (category rules + grace state)
    uploads_dir: str        # directory holding images Jake uploads into the chat
    fdc_key: str            # USDA FoodData Central API key (food database lookups)
    fatsecret_id: str       # FatSecret OAuth client id (primary food database)
    fatsecret_secret: str   # a CREDENTIAL — the FatSecret OAuth client secret; never log or render it
    speediance_email: str   # Speediance login (email) — reads only, unofficial API
    speediance_password: str  # a CREDENTIAL — never log it or render it into a result
    speediance_region: str  # account region → which cloud endpoint (EU/US/…)
    openrouter_key: str     # a CREDENTIAL — OpenRouter key for vocab import; never log or render it
    elevenlabs_key: str     # a CREDENTIAL — ElevenLabs key for Swedish TTS; never log or render it
    vocab_model: str        # model that turns a word list into full inflection tables
    tts_model: str          # ElevenLabs model id used to speak Swedish
    tts_voices: dict[str, str]  # voice label → ElevenLabs voice id (public ids, not secrets)
    heartbeat_s: float      # seconds between heartbeat ticks (fires due jobs)
    proactive_enabled: bool     # let Continuum speak up unprompted (off by default)
    proactive_interval_s: float # how often the proactive check actually evaluates
    proactive_cooldown_s: float # minimum time between surfaced proactive messages
    proactive_floor_s: float    # call the cloud tier at least this often, even if nothing changed
    proactive_significance: float # score the local gate must reach to spend a cloud call early
    proactive_tier: str         # "cloud" | "fast" — which rung makes the general "worth saying?" judgement (own setting, defaults cloud)
    routine_reminders: bool     # nudge about overdue routine steps (independent of general proactive)
    routine_grace_min: float    # minutes past a routine step's time before it counts as "missed"
    routine_quiet_start: float  # routine nudges silent from this hour (fractional ok)
    routine_quiet_end: float    # …until this hour — earlier than general quiet so morning steps can nudge
    routine_reminder_cloud: bool  # phrase routine nudges with the cloud model (natural wording)
    routine_autostart: bool     # auto-start the day's routine a set delay after Jake's first device touch
    routine_autostart_delay_min: float  # minutes after first touch (phone or PC) before auto-starting
    memory_autoextract: bool  # learn durable facts from conversation automatically
    tools_enabled: bool     # let the agent call tools to gather context (inspect/recall)
    actions_enabled: bool   # let the agent take actions (notify/open) — on by default
    clipboard_enabled: bool # let the agent read the clipboard on demand — off by default
    screen_enabled: bool    # let the agent see the screen on demand — off by default
    cloud_vision: bool      # may a screenshot go to the cloud model, when asked for
    cloud_vision_daily_max: int  # hard cap on cloud vision calls per day
    web_enabled: bool       # let the agent search the web and read pages
    calendar_ics: str       # private iCal feed URL — a CREDENTIAL, never log it
    calendar_tz: str        # timezone events are rendered in
    dev_tools_enabled: bool # let Continuum read/edit/run/restart its OWN code — on by default
    shell_enabled: bool     # let Continuum run arbitrary machine shell commands (netsh/powercfg/…) — cloud-tier only, sensitive; on by default
    reasoner_tier: str      # "fast" | "cloud" — rung that judges memory relationships
    # Waking the desk is the one capability that cannot live on the desk, so its
    # settings live here rather than in `deskops`. The MAC is the wired Realtek
    # NIC, NOT the Wi-Fi card: only the wired one is listed by `powercfg
    # -devicequery wake_armed` and only it has "Wake on Magic Packet" enabled,
    # so a magic packet aimed at the RZ616 is silently discarded.
    # There is deliberately no broadcast-address setting beside this. The phone
    # derives the directed broadcast from its own IPv4 address and prefix at send
    # time, which is correct on any network it joins; a configured value would be
    # a second source of truth that goes stale the moment the subnet changes.
    desk_mac: str           # wired NIC MAC of Jake's PC; empty disables wake

    @classmethod
    def load(cls) -> "Config":
        _load_dotenv()
        return cls(
            cloud_fast=ModelTier(
                "cloud-fast", _detect_cloud_provider(),
                # The cheap rung. Tagging, fact extraction and "is this worth
                # interrupting him for?" are small, frequent and never seen
                # directly by Jake — sending those to Opus is pure waste.
                os.getenv("CONTINUUM_FAST_MODEL", "claude-sonnet-5"),
            ),
            cloud=ModelTier(
                "cloud", _detect_cloud_provider(),
                # claude-opus-4-8 is the current top-tier model — the right target
                # for anything the local tier can't handle. Swap to claude-sonnet-5
                # for a cheaper/faster cloud rung.
                os.getenv("CONTINUUM_CLOUD_MODEL", "claude-opus-4-8"),
            ),
            claude_cli=os.getenv("CONTINUUM_CLAUDE_CLI", "claude"),
            # Runs in-process via fastembed (ONNX, no torch, no daemon). Chosen
            # over nomic-embed-text on measured agreement, not on reputation —
            # see docs/DATA.md. Changing this changes the VECTOR SPACE, so every
            # stored memory must be re-embedded or recall silently returns nothing.
            embed_model=os.getenv("CONTINUUM_EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
            memory_db=os.getenv("CONTINUUM_MEMORY_DB", str(_ROOT / "data" / "memory.db")),
            conversations_db=os.getenv("CONTINUUM_CONVERSATIONS_DB",
                                       str(_ROOT / "data" / "conversations.db")),
            scheduler_db=os.getenv("CONTINUUM_SCHEDULER_DB",
                                   str(_ROOT / "data" / "scheduler.db")),
            messages_db=os.getenv("CONTINUUM_MESSAGES_DB",
                                  str(_ROOT / "data" / "messages.db")),
            routines_db=os.getenv("CONTINUUM_ROUTINES_DB",
                                  str(_ROOT / "data" / "routines.db")),
            workouts_db=os.getenv("CONTINUUM_WORKOUTS_DB",
                                  str(_ROOT / "data" / "workouts.db")),
            activity_db=os.getenv("CONTINUUM_ACTIVITY_DB",
                                  str(_ROOT / "data" / "activity.db")),
            nutrition_db=os.getenv("CONTINUUM_NUTRITION_DB",
                                   str(_ROOT / "data" / "nutrition.db")),
            body_db=os.getenv("CONTINUUM_BODY_DB",
                              str(_ROOT / "data" / "body.db")),
            health_db=os.getenv("CONTINUUM_HEALTH_DB",
                                str(_ROOT / "data" / "health.db")),
            ledger_db=os.getenv("CONTINUUM_LEDGER_DB",
                                str(_ROOT / "data" / "ledger.db")),
            flashcards_db=os.getenv("CONTINUUM_FLASHCARDS_DB",
                                    str(_ROOT / "data" / "flashcards.db")),
            budget_db=os.getenv("CONTINUUM_BUDGET_DB",
                                str(_ROOT / "data" / "budget.db")),
            uploads_dir=os.getenv("CONTINUUM_UPLOADS_DIR",
                                  str(_ROOT / "data" / "uploads")),
            # USDA FoodData Central. DEMO_KEY works out of the box but is rate-
            # limited (~30 req/hour, 50/day per IP); a free personal key from
            # https://fdc.nal.usda.gov/api-key-signup.html lifts that. Not a
            # secret worth guarding like the Speediance password — it's a public
            # data key — but it lives in .env with the rest all the same.
            fdc_key=os.getenv("CONTINUUM_FDC_KEY", "DEMO_KEY").strip() or "DEMO_KEY",
            # FatSecret is the primary food database when these are set — a far
            # richer branded/restaurant catalogue with human-sized servings. The
            # id + secret come from a free account at platform.fatsecret.com;
            # they live in .env (the secret is a credential — never logged or
            # rendered into a result). The free tier also requires this machine's
            # IP to be whitelisted in their console, so calls must run locally.
            # Absent → USDA (above) is used, so nutrition search always works.
            fatsecret_id=os.getenv("CONTINUUM_FATSECRET_ID", "").strip(),
            fatsecret_secret=os.getenv("CONTINUUM_FATSECRET_SECRET", "").strip(),
            # Speediance is an unofficial, reverse-engineered cloud API (no dev
            # program). Creds live in .env, never in code; the password is a
            # credential and must never reach a log line or a tool result.
            speediance_email=os.getenv("SPEEDIANCE_EMAIL", "").strip(),
            speediance_password=os.getenv("SPEEDIANCE_PASSWORD", ""),
            speediance_region=os.getenv("SPEEDIANCE_REGION", "EU").strip().upper(),
            # Swedish vocab: OpenRouter turns a pasted word list into full
            # inflection tables, ElevenLabs speaks the forms worth hearing. Both
            # keys are credentials and live in .env — never in code, never in a
            # log line, never rendered into a tool result. Absent → the Flashcards
            # tab still studies existing cards; only import and audio go dark.
            openrouter_key=os.getenv("CONTINUUM_OPENROUTER_KEY", "").strip(),
            elevenlabs_key=os.getenv("CONTINUUM_ELEVENLABS_KEY", "").strip(),
            vocab_model=os.getenv("CONTINUUM_VOCAB_MODEL",
                                  "anthropic/claude-sonnet-4.5").strip(),
            tts_model=os.getenv("CONTINUUM_TTS_MODEL", "eleven_multilingual_v2").strip(),
            # Voice ids are public identifiers, not secrets, so they ship as
            # defaults and need no .env entry. andreas = Gothenburg-natural,
            # anders = standard/clear; both are wanted per card, which is why
            # this is a map rather than one id.
            tts_voices=_parse_voices(os.getenv("CONTINUUM_TTS_VOICES", "")) or {
                "andreas": "zA8eaG1JhG82rhuCip6v",
                "anders": "DSL3PSQNPbkOavwmnYl1",
            },
            heartbeat_s=float(os.getenv("CONTINUUM_HEARTBEAT_SECONDS", "15")),
            proactive_enabled=os.getenv("CONTINUUM_PROACTIVE", "0").lower()
            in ("1", "true", "yes", "on"),
            proactive_interval_s=float(os.getenv("CONTINUUM_PROACTIVE_SECONDS", "300")),
            proactive_cooldown_s=float(os.getenv("CONTINUUM_PROACTIVE_COOLDOWN", "1800")),
            proactive_floor_s=float(os.getenv("CONTINUUM_PROACTIVE_FLOOR", "1800")),
            proactive_significance=float(os.getenv("CONTINUUM_PROACTIVE_SIGNIFICANCE", "1.0")),
            # The general "is anything worth saying?" judgement gets its OWN rung,
            # decoupled from reasoner_tier and defaulting to the heavy one: it's
            # the hardest call in the system, and the significance gate already
            # keeps it rare enough that the cost is controlled.
            proactive_tier=os.getenv("CONTINUUM_PROACTIVE_TIER", "cloud").strip().lower(),
            routine_reminders=os.getenv("CONTINUUM_ROUTINE_REMINDERS", "1").lower()
            not in ("0", "false", "no", "off"),
            routine_grace_min=float(os.getenv("CONTINUUM_ROUTINE_GRACE_MIN", "45")),
            routine_quiet_start=float(os.getenv("CONTINUUM_ROUTINE_QUIET_START", "23")),
            routine_quiet_end=float(os.getenv("CONTINUUM_ROUTINE_QUIET_END", "6.5")),
            routine_reminder_cloud=os.getenv("CONTINUUM_ROUTINE_REMINDER_CLOUD", "1").lower()
            not in ("0", "false", "no", "off"),
            # Auto-start the routine a short while after Jake first touches a device
            # in the day (desktop input or phone screen-on), so the anchored
            # morning gets concrete times without him pressing "start". On by
            # default; the delay defaults to 15 minutes.
            routine_autostart=os.getenv("CONTINUUM_ROUTINE_AUTOSTART", "1").lower()
            not in ("0", "false", "no", "off"),
            routine_autostart_delay_min=float(
                os.getenv("CONTINUUM_ROUTINE_AUTOSTART_DELAY_MIN", "15")),
            memory_autoextract=os.getenv("CONTINUUM_MEMORY_AUTOEXTRACT", "1").lower()
            not in ("0", "false", "no", "off"),
            tools_enabled=os.getenv("CONTINUUM_TOOLS", "1").lower()
            not in ("0", "false", "no", "off"),
            actions_enabled=os.getenv("CONTINUUM_ACTIONS", "1").lower()
            not in ("0", "false", "no", "off"),
            clipboard_enabled=os.getenv("CONTINUUM_CLIPBOARD", "0").lower()
            in ("1", "true", "yes", "on"),
            screen_enabled=os.getenv("CONTINUUM_SCREEN", "0").lower()
            in ("1", "true", "yes", "on"),
            cloud_vision=os.getenv("CONTINUUM_CLOUD_VISION", "1").lower()
            not in ("0", "false", "no", "off"),
            cloud_vision_daily_max=int(os.getenv("CONTINUUM_CLOUD_VISION_MAX", "20")),
            web_enabled=os.getenv("CONTINUUM_WEB", "1").lower()
            not in ("0", "false", "no", "off"),
            calendar_ics=os.getenv("CONTINUUM_CALENDAR_ICS", "").strip(),
            calendar_tz=os.getenv("CONTINUUM_CALENDAR_TZ", "Europe/London").strip(),
            dev_tools_enabled=os.getenv("CONTINUUM_DEV_TOOLS", "1").lower()
            not in ("0", "false", "no", "off"),
            shell_enabled=os.getenv("CONTINUUM_SHELL", "1").lower()
            not in ("0", "false", "no", "off"),
            reasoner_tier=os.getenv("CONTINUUM_REASONER", "fast").strip().lower(),
            desk_mac=os.getenv("CONTINUUM_DESK_MAC", "").strip(),
        )

    @property
    def has_cloud_key(self) -> bool:
        """True when a metered Anthropic API key is available."""
        return bool(os.getenv("ANTHROPIC_API_KEY"))

    @property
    def cloud_available(self) -> bool:
        """True when the cloud tier can actually be reached, by either path."""
        if self.cloud.provider == "anthropic":
            return self.has_cloud_key
        if self.cloud.provider == "claude-cli":
            return shutil.which(self.claude_cli) is not None
        return False
