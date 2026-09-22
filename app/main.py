import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.api import router
from app.imports import generate_templates
from app.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def _documentation_paths(demo_mode: bool) -> dict[str, str | None]:
    return {
        "docs_url": "/docs" if demo_mode else None,
        "redoc_url": "/redoc" if demo_mode else None,
        "openapi_url": "/openapi.json" if demo_mode else None,
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_settings().ensure_runtime()
    template_dir = Path("assets/import_templates")
    required_templates = (
        template_dir / "customer_list_template.xlsx",
        template_dir / "price_list_template.xlsx",
    )
    if not all(path.exists() for path in required_templates):
        generate_templates(template_dir)
    yield


settings = get_settings()
app = FastAPI(
    title="AI Sales Agent MVP",
    version="0.1.0",
    description="Bounded email workflow: AI extracts/drafts; deterministic code controls prices and sends.",
    lifespan=lifespan,
    **_documentation_paths(settings.demo_mode),
)
app.include_router(router)
