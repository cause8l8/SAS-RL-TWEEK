"""Minimal local compatibility shim used by bundled CustomTkinter.

The application explicitly selects dark mode, so platform theme monitoring is
unnecessary. Keeping the tiny public surface avoids an extra runtime package.
"""


def theme() -> str:
    return "Dark"


def isDark() -> bool:  # noqa: N802 - mirrors darkdetect's public API
    return True


def isLight() -> bool:  # noqa: N802 - mirrors darkdetect's public API
    return False
