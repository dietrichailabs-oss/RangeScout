from __future__ import annotations

from dataclasses import replace

from app.catalysts.entities import CatalystEvent


KEYWORDS = {
    "halt": ("halt", "resumption", "resume trading"),
    "sec_filing": ("8-k", "10-q", "10-k", "s-3", "424b", "13d", "13g", "form 4", "form 144"),
    "government_policy": ("tariff", "sanction", "export control", "executive order", "memorandum", "proclamation"),
    "legislation": ("bill", "house passed", "senate passed", "signed into law", "veto"),
}


def classify(event: CatalystEvent) -> CatalystEvent:
    text = f"{event.title} {event.summary or ''}".lower()
    category = next((name for name, words in KEYWORDS.items() if any(word in text for word in words)), "news")
    urgency = "critical" if category == "halt" else "high" if category in {"sec_filing", "government_policy"} else "normal"
    positive = any(word in text for word in ("approval", "resumed", "award", "beat", "signed"))
    negative = any(word in text for word in ("halted", "sanction", "investigation", "miss", "veto"))
    direction = "mixed" if positive and negative else "potentially positive" if positive else "potentially negative" if negative else "unclear"
    return replace(event, category=category, urgency=urgency, direction=direction)
