"""Swedish vocab services — turning a pasted word list into real cards, and
speaking the forms worth hearing.

Two external APIs, both optional. Continuum works without either: the Flashcards
tab still studies whatever is already stored, and only *import* and *audio* go
dark. That is deliberate — a missing key must degrade one feature, never the tab.

**Import** (OpenRouter). A word list is not a flashcard. "bok - book" has to
become a lemma, a gender, an inflection table, an example sentence, and the list
of surface forms worth speaking. The prompt is carried over from Glosor's
`server.js`, which had been tuned against real Rivstart and Duolingo lists — its
specificity about genitives and irregulars is the reason the output is usable,
so it is ported rather than rewritten.

**Audio** (ElevenLabs). Two voices per form: one Gothenburg-natural, one
standard/clear. Both, because hearing only one accent teaches you to understand
only one accent.

Cost is the thing that shapes this module. ElevenLabs bills per character and
Jake's tier has a fixed monthly pool, so:

  * Audio is **never** generated as a side effect of import. A 200-card import
    would be ~1,600 synthesis calls, spent before he has decided the cards are
    even right. Voicing is a separate, explicit action.
  * Identical strings are synthesised **once**. "en bok" appearing on four cards
    is one call, not four — the store keys audio by (card, form), but the spend
    is keyed by the string.
  * Every batch takes a hard `limit`. An unbounded loop over a big deck is how a
    monthly quota disappears in one request.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Iterable, Optional

import httpx

from .config import Config
from .flashcards import FlashcardStore

log = logging.getLogger("continuum.vocab")

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

# Ported verbatim from glosor/server.js. The detail is load-bearing: without the
# explicit genitive rules and the irregular examples, models return plausible but
# wrong tables (*bokens* vs *bokens*, *gick* regularised to *gådde*).
IMPORT_SYSTEM = """You are a Swedish lexicographer building flashcards for an English speaker (sources: Duolingo, Rivstart A1/A2).

For EACH distinct Swedish item output an object with EXACTLY these fields:
- "swedish": clean dictionary/lemma form. Nouns: indefinite singular WITHOUT article (e.g. "bok"). Verbs: infinitive WITHOUT "att" (e.g. "prata"). Others: as written.
- "english": concise, natural English translation.
- "pos": one of "noun","verb","adjective","adverb","phrase","number","pronoun","preposition","conjunction","interjection","other".
- "gender": for nouns "en" or "ett"; otherwise null.
- "tags": array of lowercase tags. ALWAYS include the pos. For nouns ALSO include the gender ("en" or "ett").
- "example_sv": short natural Swedish example sentence using the item.
- "example_en": English translation of that sentence.
- "forms": the full inflection table (use correct Swedish inflection, including irregulars):
   * NOUN -> {"indef_sg","def_sg","indef_pl","def_pl","gen_sg","gen_pl"}
       indef_sg/def_sg/indef_pl/def_pl e.g. bok -> boken -> böcker -> böckerna.
       gen_sg = def_sg + "s" (boken -> bokens, huset -> husets).
       gen_pl = def_pl + "s" (böckerna -> böckernas, husen -> husens).
       If the noun has no plural, set indef_pl, def_pl, gen_pl to null (gen_pl too).
   * VERB -> {"infinitive","present","past","supine","imperative"}
       e.g. prata -> pratar -> pratade -> pratat -> prata;  gå -> går -> gick -> gått -> gå.
   * ADJECTIVE -> {"en_form","ett_form","plural_form","comparative","superlative"}
       e.g. stor -> stort -> stora -> större -> störst. Use null for any that don't exist.
   * anything else -> null.
- "speak": array of the surface strings that should get spoken audio (natural spoken forms):
   * NOUN: ["en bok" or "ett hus" (indefinite WITH article), the def_sg, indef_pl, def_pl] (skip nulls).
   * VERB: [infinitive, present, past, supine].
   * ADJECTIVE: [en_form, comparative, superlative].
   * others: [the swedish item].

Rules: correct typos, restore Swedish diacritics (å ä ö), fix wrong supplied translations, deduplicate.
Output ONLY a JSON object {"cards":[ ... ]} with no commentary or code fences."""


class VocabError(RuntimeError):
    """Something external failed. Carries a message safe to show Jake — never a
    key, never a raw provider body that might echo one back."""


class VocabService:
    def __init__(self, cfg: Config, store: FlashcardStore) -> None:
        self._cfg = cfg
        self._store = store

    # ---- capability ---------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """What the tab can offer right now. Booleans only — never key material,
        not even a length, since this is rendered in a browser."""
        return {
            "import": bool(self._cfg.openrouter_key),
            "audio": bool(self._cfg.elevenlabs_key),
            "model": self._cfg.vocab_model,
            "voices": list(self._cfg.tts_voices or {}),
        }

    # ---- import -------------------------------------------------------------
    async def build_cards(self, text: str) -> list[dict[str, Any]]:
        """Turn a pasted word list into card dicts. Does NOT store them — the
        caller reviews first, because a model that mis-genders a noun should be
        caught before it becomes 40 cards Jake has to re-learn."""
        text = (text or "").strip()
        if not text:
            return []
        if not self._cfg.openrouter_key:
            raise VocabError("No OpenRouter key configured — set CONTINUUM_OPENROUTER_KEY in .env.")
        payload = {
            "model": self._cfg.vocab_model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": IMPORT_SYSTEM},
                         {"role": "user", "content": text}],
        }
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                r = await client.post(
                    _OPENROUTER_URL, json=payload,
                    headers={"Authorization": f"Bearer {self._cfg.openrouter_key}",
                             "HTTP-Referer": "https://continuum.local",
                             "X-Title": "Continuum"})
        except httpx.HTTPError as e:
            raise VocabError(f"Could not reach OpenRouter ({type(e).__name__}).") from e
        if r.status_code != 200:
            # Status only. A provider error body can quote the request back,
            # which on an auth failure means quoting the key.
            raise VocabError(f"OpenRouter returned HTTP {r.status_code}.")
        try:
            content = r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as e:
            raise VocabError("OpenRouter returned an unreadable response.") from e
        return _parse_cards(content)

    # ---- audio --------------------------------------------------------------
    async def synth(self, text: str, voice: str) -> bytes:
        """One string, one voice, mp3 bytes back."""
        if not self._cfg.elevenlabs_key:
            raise VocabError("No ElevenLabs key configured — set CONTINUUM_ELEVENLABS_KEY in .env.")
        voice_id = (self._cfg.tts_voices or {}).get(voice)
        if not voice_id:
            raise VocabError(f"Unknown voice {voice!r}.")
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(
                    _ELEVEN_URL.format(voice_id=voice_id),
                    json={"text": text, "model_id": self._cfg.tts_model},
                    headers={"xi-api-key": self._cfg.elevenlabs_key,
                             "Content-Type": "application/json"})
        except httpx.HTTPError as e:
            raise VocabError(f"Could not reach ElevenLabs ({type(e).__name__}).") from e
        if r.status_code != 200:
            raise VocabError(f"ElevenLabs returned HTTP {r.status_code}.")
        return r.content

    async def voice_cards(self, card_ids: Optional[Iterable[str]] = None, *,
                          limit: int = 40) -> dict[str, Any]:
        """Generate the missing audio for some cards, newest-first, capped.

        `limit` counts **synthesis calls**, not cards, because that is what is
        actually billed. Returning `remaining` lets the tab offer "do the next
        batch" instead of silently stopping — a cap that isn't reported reads
        exactly like the work being finished.
        """
        if not self._cfg.elevenlabs_key:
            raise VocabError("No ElevenLabs key configured — set CONTINUUM_ELEVENLABS_KEY in .env.")
        wanted = await asyncio.to_thread(self._store.missing_audio)
        if card_ids is not None:
            keep = {str(c) for c in card_ids}
            wanted = [w for w in wanted if w["id"] in keep]

        # Flatten to (card, form, voice) jobs, then collapse by the *string*:
        # the same surface form on five cards is one synthesis, five writes.
        jobs: list[tuple[str, str, str]] = [
            (w["id"], w["form"], v) for w in wanted for v in w["voices"]]
        by_text: dict[tuple[str, str], list[str]] = {}
        for card_id, form, voice in jobs:
            by_text.setdefault((form, voice), []).append(card_id)

        keys = list(by_text)
        todo, deferred = keys[:max(0, int(limit))], keys[max(0, int(limit)):]
        made = written = 0
        errors: list[str] = []
        for form, voice in todo:
            try:
                data = await self.synth(form, voice)
            except VocabError as e:
                errors.append(f"{form} ({voice}): {e}")
                continue
            made += 1
            for card_id in by_text[(form, voice)]:
                try:
                    await asyncio.to_thread(self._store.put_audio, card_id, form, voice, data)
                    written += 1
                except Exception as e:      # one bad card must not abort the batch
                    log.warning("could not attach audio to %s", card_id, exc_info=True)
                    errors.append(f"{form} -> {card_id}: {type(e).__name__}")
        return {"synthesised": made, "attached": written,
                # Both numbers, because they answer different questions: calls is
                # what the next batch will cost, writes is how much silent audio
                # is still outstanding.
                "remaining_calls": len(deferred),
                "remaining_writes": sum(len(by_text[k]) for k in deferred),
                "chars": sum(len(f) for f, _ in todo),
                "errors": errors[:10]}


def _parse_cards(content: str) -> list[dict[str, Any]]:
    """Pull the card array out of a model response.

    Tolerates a fenced or chatty reply by falling back to the outermost
    {...} span, the same two-step Glosor used — `response_format` makes this
    rare but not impossible, and losing a whole import to one stray sentence
    is a bad trade."""
    try:
        data = json.loads(content)
    except ValueError:
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            raise VocabError("The import model did not return JSON.")
        try:
            data = json.loads(content[start:end + 1])
        except ValueError as e:
            raise VocabError("The import model did not return JSON.") from e
    cards = data.get("cards") if isinstance(data, dict) else None
    if not isinstance(cards, list):
        raise VocabError("The import model returned no cards.")
    return [c for c in cards if isinstance(c, dict)
            and str(c.get("swedish") or "").strip()
            and str(c.get("english") or "").strip()]
