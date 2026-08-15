"""Minimal production bootstrap for the ``crew`` console and module entry."""

from __future__ import annotations

from crew.process_hardening import harden_main_process

harden_main_process("cli")


def main() -> None:
    from crew.cli.main import main as cli_main

    cli_main()
