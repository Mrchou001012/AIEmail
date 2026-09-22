"""Executable guardrails for the gradual services.py decomposition."""

from __future__ import annotations

import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
LEGACY_SERVICES_MAX_LINES = 250
DISPOSITION_SERVICE_MAX_LINES = 1_200
SPLIT_SERVICE_MODULES = (
    "handoffs/agent_resume.py",
    "inbound/automated_reply_service.py",
    "catalogs/backfill_types.py",
    "inbound/bounce_service.py",
    "catalogs/category_product_list.py",
    "coa/coa_service.py",
    "quotations/commercial_service.py",
    "research/company_research_context.py",
    "research/company_research_service.py",
    "delivery/contact_delivery_service.py",
    "delivery/delivery_safety.py",
    "common/demo_service.py",
    "inbound/email_ingestion.py",
    "common/email_threading.py",
    "handoffs/forwarding_service.py",
    "catalogs/general_product_list.py",
    "handoffs/handoff_prepared_preview.py",
    "handoffs/handoff_preview.py",
    "handoffs/handoff_service.py",
    "handoffs/human_reply_service.py",
    "inbound/inbound_followup.py",
    "inbound/inbound_processing.py",
    "inbound/inquiry_matching.py",
    "inbound/inquiry_resolution.py",
    "quotations/manual_quote_service.py",
    "quotations/multi_product_quote.py",
    "common/notifications.py",
    "delivery/outbound_delivery.py",
    "delivery/outbound_guards.py",
    "delivery/outbound_pacing.py",
    "delivery/outbound_recovery.py",
    "quotations/outreach_service.py",
    "quotations/prepared_quote_service.py",
    "catalogs/product_list_backfill.py",
    "catalogs/product_list_backfill_apply.py",
    "catalogs/product_list_backfill_prepare.py",
    "catalogs/product_list_service.py",
    "quotations/quote_clarification.py",
    "quotations/quote_context.py",
    "inbound/reactivation_threading.py",
    "common/service_core.py",
    "quotations/single_product_quote.py",
)
INDEPENDENT_DOMAIN_MODULES = SPLIT_SERVICE_MODULES + (
    "coa/coa_delivery.py",
    "dispositions/disposition_actions.py",
    "dispositions/disposition_audit.py",
    "dispositions/disposition_batches.py",
    "dispositions/disposition_planning.py",
    "dispositions/disposition_resolution.py",
    "dispositions/disposition_service.py",
    "common/email_identity.py",
    "quotations/quote_rendering.py",
    "jobs.py",
)


def test_legacy_services_facade_cannot_grow() -> None:
    lines = (APP_DIR / "services.py").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= LEGACY_SERVICES_MAX_LINES, (
        "app/services.py is a frozen compatibility facade. Put new behavior in "
        "a focused domain module or extract more legacy code before adding glue."
    )


def test_split_service_modules_remain_focused() -> None:
    oversized = {
        name: len((APP_DIR / name).read_text(encoding="utf-8").splitlines())
        for name in SPLIT_SERVICE_MODULES
        if len((APP_DIR / name).read_text(encoding="utf-8").splitlines()) > 500
    }
    assert not oversized, f"Extract a cohesive workflow phase before growing these modules: {oversized}"


def test_split_services_have_no_eager_import_cycles() -> None:
    """Lazy job dispatch is allowed; eager capability imports must form a DAG."""
    modules = {f"app.{name.removesuffix('.py').replace('/', '.')}": name for name in INDEPENDENT_DOMAIN_MODULES}
    dependencies: dict[str, set[str]] = {}
    for module, filename in modules.items():
        tree = ast.parse((APP_DIR / filename).read_text(encoding="utf-8"))
        dependencies[module] = {node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module in modules}
    visited: set[str] = set()

    def visit(module: str, path: tuple[str, ...] = ()) -> None:
        assert module not in path, "Eager import cycle: " + " -> ".join((*path, module))
        if module in visited:
            return
        for dependency in dependencies[module]:
            visit(dependency, (*path, module))
        visited.add(module)

    for module in dependencies:
        visit(module)


def test_disposition_orchestrator_cannot_regrow_into_a_monolith() -> None:
    lines = (APP_DIR / "dispositions/disposition_service.py").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= DISPOSITION_SERVICE_MAX_LINES, (
        "app/dispositions/disposition_service.py coordinates the workflow; put identity "
        "resolution, plan rendering, actions, or audit rollback in their "
        "focused disposition modules instead of growing the orchestrator."
    )


def test_extracted_domain_modules_do_not_depend_on_services_facade() -> None:
    violations: list[str] = []
    for filename in INDEPENDENT_DOMAIN_MODULES:
        path = APP_DIR / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.services":
                violations.append(f"{filename}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "app" and any(alias.name == "services" for alias in node.names):
                violations.append(f"{filename}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.services":
                        violations.append(f"{filename}:{node.lineno}")
    assert not violations, "Extracted domain modules must not import the legacy services facade: " + ", ".join(violations)
