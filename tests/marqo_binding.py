"""Binding sentinel for the Marqo test fakes.

Every Marqo fake in this suite binds through *client construction*: either
``monkeypatch.setattr(marqo, "Client", _Fake)`` or
``monkeypatch.setitem(sys.modules, "marqo", _fake_module)``. Both work only
because the production code does a function-local ``import marqo`` at the call
site. Consolidating Marqo access behind ``pipeline.vector_store`` moves that
import, and any fake still aimed at the old seam silently stops binding.

That is dangerous for assertions written as PURE NEGATIVES — "Marqo was never
searched", "no index was deleted". A negative over an empty recorder is
vacuously true, so a fake that never bound turns the regression guard into a
no-op that still reports green.

This sentinel is the positive counterweight: the fake client records that it
was *constructed at all*, so a test can assert "the code under test built its
client through our fake" before asserting what the fake never saw.

It lives in its own module (not ``tests/conftest.py``) so both
``test_tenant_isolation.py`` and ``test_activities.py`` can import it without
touching conftest's process-wide setup and without an import cycle: this module
imports nothing from the package under test.
"""

from __future__ import annotations


class MarqoBindingSentinel:
    """Records every construction of a fake Marqo client."""

    def __init__(self) -> None:
        self.constructions: list[str | None] = []

    def record(self, url: str | None = None) -> None:
        """Called from the fake client's ``__init__``."""
        self.constructions.append(url)

    def reset(self) -> None:
        self.constructions.clear()

    @property
    def constructed(self) -> bool:
        return bool(self.constructions)

    def assert_constructed(self, why: str = "") -> None:
        assert self.constructions, (
            "the fake Marqo client was NEVER constructed — the fake no longer "
            "binds to the code under test, so any 'this never happened' "
            "assertion here is vacuously true. " + why
        )


#: Shared instance. Each test fixture resets it before use.
MARQO_BINDING = MarqoBindingSentinel()
