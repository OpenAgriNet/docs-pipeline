"""Binding sentinel for the Marqo test fake.

``tests.helpers.FakeMarqo`` binds by replacing ``sys.modules["marqo"]``, which
works only because production builds its client through a function-local
``import marqo`` in ``pipeline.vector_store``. If that import moves, the fake
silently stops binding.

That is dangerous for assertions written as PURE NEGATIVES ("Marqo was never
searched", "no index was deleted"). A negative over an empty recorder is
vacuously true, so a fake that never bound turns the regression guard into a
no-op that still reports green.

This sentinel is the positive counterweight: the fake records that it was
*constructed at all*, so a test can assert "the code under test built its client
through our fake" before asserting what the fake never saw. ``FakeMarqo``
inherits it. It imports nothing from the package under test.
"""

from __future__ import annotations


class MarqoBindingSentinel:
    """Records every construction of a fake Marqo client."""

    def __init__(self) -> None:
        self.constructions: list[str | None] = []

    def record(self, url: str | None = None) -> None:
        """Called from the fake client's constructor."""
        self.constructions.append(url)

    def reset(self) -> None:
        self.constructions.clear()

    @property
    def constructed(self) -> bool:
        return bool(self.constructions)

    def assert_constructed(self, why: str = "") -> None:
        assert self.constructions, (
            "the fake Marqo client was NEVER constructed: the fake no longer "
            "binds to the code under test, so any 'this never happened' "
            "assertion here is vacuously true. " + why
        )
