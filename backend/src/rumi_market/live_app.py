"""Explicit live ASGI entry; the original create_app default stays disabled."""

from .app import DEFAULT_CATALOG, create_app
from .live_runtime import LiveProviderFactory


def create_live_app(*, catalog_path=DEFAULT_CATALOG, apk_path=None,
                    provider_factory=None, login_timeout=300,
                    remember_store=None):
    return create_app(provider_factory=provider_factory or LiveProviderFactory(apk_path=apk_path),
                      catalog_path=catalog_path, login_timeout=login_timeout,
                      remember_store=remember_store)


app = create_live_app()
