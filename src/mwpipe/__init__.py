"""mwpipe: install a Nexus Mods collection for OpenMW and run the usual post-processing tools."""

from importlib.metadata import PackageNotFoundError, version

try:  # the version is declared once, in pyproject.toml
    __version__ = version("mwpipe")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
