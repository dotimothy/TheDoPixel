"""TheDoPixel application package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pixel-relay")
except PackageNotFoundError:
    __version__ = "0.1.0"
