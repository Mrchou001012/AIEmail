"""Executable guardrails for the gradual services.py decomposition."""

from __future__ import annotations

import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
LEGACY_SERVICES_MAX_LINES = 9_800
INDEPENDENT_DOMAIN_MODULES = (
    "coa_delivery.py",
    "disposition_batches.py",
    "disposition_service.py",
    "email_identity.py",
    "quote_rendering.py",
)


def test_legacy_services_facade_cannot_grow() -> None:
    lines = (APP_DIR / "services.py").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= LEGACY_SERVICES_MAX_LINES, (
        "app/services.py is a frozen compatibility facade. Put new behavior in "
        "a focused domain module or extract more legacy code before adding glue."
    )


def test_extracted_domain_modules_do_not_depend_on_services_facade() -> None:
    violations: list[str] = []
    for filename in INDEPENDENT_DOMAIN_MODULES:
        path = APP_DIR / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.services":
                violations.append(f"{filename}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.services":
                        violations.append(f"{filename}:{node.lineno}")
    assert not violations, (
        "Extracted domain modules must not import the legacy services facade: "
        + ", ".join(violations)
    )
