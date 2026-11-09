"""A step's user-facing text (`docs/analysis-python-steps.md` §3.5).

Every string a person sees (the step's name, purpose and help, parameter labels and help, choice
labels, error sentences) is a message with an English source, a risk (`ordinary` or
`technical`) and protected tokens that every translation keeps unchanged (model names, units,
identifiers). A step may carry drafts for the configured locales; each draft names the exact
source revision it translates, so a changed English source leaves the draft stale and the
English shows. Drafts are drafts: approval is recorded outside the manifest by a lab or
community review, never by the author.

    name = Text("Glitch finder")
    params = {"shortest": Quantity("time", "s", label=Text("Shortest valid pulse", tokens=[]))}
    translations = {"de": {"name": "Glitch-Sucher"}}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .canonical import canonical_hex

LOCALES = ("nb", "zh-Hans", "uk", "fr", "de", "es")


@dataclass(frozen=True)
class Text:
    """One message: English source, risk, protected tokens and an optional note for translators."""

    en: str
    technical: bool = False
    tokens: Sequence[str] = field(default_factory=tuple)
    context: str | None = None

    def to_json(self) -> dict:
        out: dict = {"en": self.en, "risk": "technical" if self.technical else "ordinary", "tokens": list(self.tokens)}
        if self.context is not None:
            out["context"] = self.context
        return out


def text(value: Text | str) -> Text:
    return value if isinstance(value, Text) else Text(value)


def revision(message: dict) -> str:
    """The source revision a draft names: SHA-256 of the message's canonical JSON."""
    return canonical_hex(message)
