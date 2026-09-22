from app.main import _documentation_paths


def test_production_disables_api_documentation() -> None:
    assert _documentation_paths(False) == {
        "docs_url": None,
        "redoc_url": None,
        "openapi_url": None,
    }


def test_demo_mode_enables_api_documentation() -> None:
    assert _documentation_paths(True) == {
        "docs_url": "/docs",
        "redoc_url": "/redoc",
        "openapi_url": "/openapi.json",
    }
