"""Error types shared across tabdiff."""


class TabDiffError(Exception):
    """Base class for all tabdiff errors. Maps to exit code 2."""

    exit_code = 2


class SourceError(TabDiffError):
    """A data source could not be opened, read, or understood."""


class SchemaIncompatible(TabDiffError):
    """The two schemas are too different to diff meaningfully."""


class KeyNotUnique(TabDiffError):
    """The chosen key is not unique on at least one side."""


class NoKeyFound(TabDiffError):
    """No key was given and none could be guessed."""
