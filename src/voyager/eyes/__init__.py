"""Eyes: typed perception. ``JevEyes`` talks to TypeSafe and answers every question; ``ClaudeEyes``
answers the same typed questions only when a Jev call fails (see ``fallback.JevWithFallback``)."""
from .base import Eyes, choice, noul, box, score, FakeEyes  # noqa: F401


def make_eyes(settings):
    """Jev is required. Claude is wired in as the fallback when an Anthropic key is available."""
    import os

    from .fallback import JevWithFallback
    from .jev import JevEyes

    jev = JevEyes(settings)
    fallback = None
    if settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"):
        from .claude import ClaudeEyes

        fallback = ClaudeEyes(settings)
    return JevWithFallback(jev, fallback)
