from __future__ import annotations


def main() -> None:
    from .runner import main as _main

    _main()


def run() -> None:
    from .runner import run as _run

    _run()


__all__ = ["main", "run"]
