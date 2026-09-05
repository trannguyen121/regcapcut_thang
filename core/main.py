"""Application entrypoint."""

from core.orchestrator import Orchestrator
from core.settings import load_settings


def main() -> None:
    """Start the application."""
    orchestrator = Orchestrator(load_settings())
    orchestrator.run()
    print("Auto started")


if __name__ == "__main__":
    main()
