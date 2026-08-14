"""Interface for ``python -m dls_motor_scanning``."""

from .cli import app

__all__ = ["app"]


if __name__ == "__main__":
    app()
