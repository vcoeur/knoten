"""knoten — standalone CLI zettelkasten with optional remote-backend sync."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("knoten")
except PackageNotFoundError:  # pragma: no cover — running from a non-installed checkout
    __version__ = "0.0.0+unknown"
