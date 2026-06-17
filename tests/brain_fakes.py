"""Shared test helpers for the brain's plan-reaction classification (v0.0.25).

Production code no longer keyword-matches the user's reply to a proposed plan:
the brain INTERPRETS it (``relay.conversation._classify_reaction``) and emits a
``<reaction>`` tag. Test fakes stand in for that judgment -- given the
reaction-classification call's messages, they return the ``<reaction>`` the
scripted user reply implies. This keeps the seam (the model interprets) intact
while staying network-free and deterministic.

This is NOT collected by pytest (no ``test_`` prefix); test modules import it.
"""

from __future__ import annotations

# A unique phrase from _REACTION_SYSTEM, so a fake can recognize the reaction call.
REACTION_MARKER = "INTERPRET their reply"

# The fake's stand-in for the model's judgment. The PRODUCTION gate is the model's
# interpretation; here the test double maps a scripted reply to a category.
_AFFIRM = {
    "", "ok", "okay", "yes", "y", "go", "go for it", "commit", "approve", "approved",
    "looks good", "lgtm", "ship it", "ship", "do it", "proceed", "sounds good",
    "perfect", "great", "good", "done", "yep", "yeah", "build it",
    "yes that looks perfect", "yes that's perfect", "yes perfect",
}
_REJECT = {"no", "nope", "cancel", "scrap it", "scrap", "stop", "abort", "no cancel"}


def is_reaction_call(system: str) -> bool:
    """Whether ``system`` (the call's flattened system prompt) is the reaction call."""
    return REACTION_MARKER in system


def extract_user_reply(messages) -> str:
    """Pull the user's plan reply out of the reaction-classification user message."""
    user = messages[-1]["content"]
    marker = "The user's reply:\n"
    if marker in user:
        return user.split(marker, 1)[1].split("\n\n", 1)[0].strip()
    return ""


def reaction_reply(user_reply: str) -> str:
    """Return the ``<reaction>`` tag the scripted ``user_reply`` implies."""
    normalized = (user_reply or "").strip().lower().rstrip("!.")
    if normalized in _AFFIRM:
        return "<reaction>approve</reaction>"
    if normalized in _REJECT:
        return "<reaction>reject</reaction>"
    # Everything else is treated as a revision request carrying the reply as feedback.
    return f"<reaction>revise</reaction><change>{user_reply}</change>"


def reaction_for_messages(messages) -> str:
    """Convenience: classify directly from a call's messages."""
    return reaction_reply(extract_user_reply(messages))
