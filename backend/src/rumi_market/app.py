from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from .auth import create_auth_router, provider_capabilities, require_session, set_session_cookie
from .catalog import Catalog
from .errors import ApiError
from .home_production import DEFAULT_HOME_CATALOG, HomeProductionCatalog, HomeProductionView
from .home_management import HomeManagementService
from .home_models import HomeAccountView, HomeSettingsRequest
from .order_models import OrderPlanRequest, OrderResolveRequest, OrderSettings, OrderStartRequest
from .order_service import OrderService
from .market import MarketService
from .models import Category, MarketSnapshot, PetCatalogResponse, PetDetail, RefreshRequest
from .pet_details import PetDetails
from .providers import GameProvider, UnverifiedGameProvider
from .public_images import PUBLIC_IMAGE_CACHE_CONTROL, public_png
from .rush import RushPurchaseService, RushPurchaseState, RushStartRequest, RushStopRequest
from .redbag_models import RedbagStartRequest
from .redbag_service import RedbagService
from .sessions import SessionStore
DEFAULT_CATALOG = Path(__file__).resolve().parents[3] / "data" / "pets" / "catalog.json"


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def create_app(*, provider_factory: Callable[[], GameProvider] = UnverifiedGameProvider,
               catalog_path: Path = DEFAULT_CATALOG, login_timeout: float = 30,
               market_timeout: float = 30, home_catalog_path: Path = DEFAULT_HOME_CATALOG,
               remember_store=None) -> FastAPI:
    if type(login_timeout) not in (int, float) or not 0 < login_timeout <= 300:
        raise ValueError("Invalid login timeout")
    if type(market_timeout) not in (int, float) or not 0 < market_timeout <= 30:
        raise ValueError("Invalid market timeout")
    catalog = Catalog(Path(catalog_path))
    pet_details = PetDetails(catalog)
    home_production = HomeProductionCatalog(home_catalog_path, catalog.items)
    sessions = SessionStore(provider_factory)
    home_management = HomeManagementService(catalog, sessions)
    orders = OrderService(sessions, item_catalog=catalog.items, pet_catalog=catalog)
    market = MarketService(catalog, sessions)
    rush = RushPurchaseService(catalog, sessions)
    redbags = RedbagService(sessions)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        await sessions.close()
        catalog.close()

    application = FastAPI(title="Rumi Market", lifespan=lifespan)
    application.state.sessions = sessions
    application.state.catalog = catalog
    application.state.rush = rush
    application.state.redbags = redbags
    application.state.orders = orders

    @application.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError):
        return JSONResponse(error.body(), status_code=error.status_code)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # FastAPI's default errors contain input values, including credentials.
        return JSONResponse(ApiError("INVALID_INPUT", "输入格式不合法", 422).body(), status_code=422)

    @application.middleware("http")
    async def same_origin(request: Request, call_next):
        if (request.method in {"POST", "PUT", "PATCH", "DELETE"}
                or request.url.path in {"/api/auth/login-challenge", "/api/auth/captcha"}):
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site")
            try:
                rejected = bool(origin and _origin(origin) != _origin(str(request.base_url)))
            except ValueError:
                rejected = True
            if rejected or fetch_site in {"cross-site", "same-site"}:
                error = ApiError("ORIGIN_REJECTED", "请求来源不受允许", 403)
                return JSONResponse(error.body(), status_code=403, headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        created_session = getattr(request.state, "created_session", None)
        if created_session and sessions.get(created_session):
            set_session_cookie(response, request, created_session)
        public_image = (
            request.method == "GET"
            and request.scope.get("endpoint") in (pet_image, skill_image)
            and response.status_code in {200, 304}
            and response.headers.get("cache-control") == PUBLIC_IMAGE_CACHE_CONTROL
            and bool(response.headers.get("etag"))
            and "set-cookie" not in response.headers
            and (response.status_code == 304 or response.headers.get("content-type") == "image/png")
        )
        if request.url.path.startswith("/api/") and not public_image:
            response.headers["Cache-Control"] = "no-store"
        if (request.url.path.startswith('/api/') and not public_image
                and request.cookies.get('rumi_remember')):
            response.delete_cookie('rumi_remember', path='/', httponly=True,
                                   samesite='strict', secure=request.url.scheme == 'https')
        return response

    application.include_router(create_auth_router(sessions, login_timeout=login_timeout))

    @application.get("/api/time")
    async def server_time():
        return {"unix_ms": time.time_ns() // 1_000_000, "time_zone": "Asia/Shanghai"}

    @application.get("/api/health")
    async def health():
        capabilities = await provider_capabilities(sessions)
        return {"service": "rumi-market", "status": "ok",
                "game_integration_ready": capabilities.integration_ready}

    @application.get("/api/pets", response_model=PetCatalogResponse)
    async def get_pets(query: str = ""):
        return pet_details.catalog_query(query)

    @application.get('/api/home/production', response_model=HomeProductionView)
    async def get_home_production(request: Request):
        require_session(sessions, request, connected=True)
        return home_production.snapshot()

    @application.get('/api/home/state', response_model=HomeAccountView)
    async def get_home_state(request: Request):
        session = require_session(sessions, request, connected=True)
        return await home_management.state(session)

    @application.put('/api/home/settings', response_model=HomeAccountView)
    async def apply_home_settings(body: HomeSettingsRequest, request: Request):
        session = require_session(sessions, request, connected=True)
        async with orders.home_lease(session):
            return await home_management.apply(session, body)

    @application.get('/api/home/orders/state')
    async def order_state(request: Request):
        return await orders.state(require_session(sessions, request, connected=True))

    @application.get('/api/redbags/task')
    async def redbag_state(request: Request):
        return await redbags.state(require_session(sessions, request, connected=True))

    @application.post('/api/redbags/task/start')
    async def redbag_start(body: RedbagStartRequest, request: Request):
        return await redbags.start(require_session(sessions, request, connected=True), body.request_id)

    @application.post('/api/redbags/task/stop')
    async def redbag_stop(request: Request):
        return await redbags.stop(require_session(sessions, request, connected=True))

    @application.put('/api/home/orders/settings')
    async def order_settings(body: OrderSettings, request: Request):
        return await orders.save_settings(require_session(sessions, request, connected=True), body)

    @application.post('/api/home/orders/plan')
    async def order_plan(body: OrderPlanRequest, request: Request):
        return await orders.plan(require_session(sessions, request, connected=True), body.expected_revision, body.settings)

    @application.post('/api/home/orders/task/start')
    async def order_start(body: OrderStartRequest, request: Request):
        return await orders.start(require_session(sessions, request, connected=True), body.request_id, body.expected_revision)

    @application.post('/api/home/orders/task/stop')
    async def order_stop(request: Request):
        return await orders.stop(require_session(sessions, request, connected=True))

    @application.post('/api/home/orders/inspection/start')
    async def inspection_start(body: OrderStartRequest, request: Request):
        return await orders.start(require_session(sessions, request, connected=True), body.request_id,
                                  body.expected_revision, kind='inspection')

    @application.post('/api/home/orders/task/resolve')
    async def order_resolve(body: OrderResolveRequest, request: Request):
        return await orders.resolve(require_session(sessions, request, connected=True), body.expected_revision)

    @application.get("/api/pets/{pet_id}", response_model=PetDetail)
    async def get_pet(pet_id: str):
        detail = pet_details.lookup(pet_id)
        if detail is None:
            raise ApiError("PET_NOT_FOUND", "宠物未收录", 404)
        return detail

    @application.get("/api/pet-skills/{skill_id}/image")
    async def skill_image(skill_id: str, request: Request):
        path = pet_details.image_path(skill_id)
        if path is None:
            raise ApiError("IMAGE_NOT_FOUND", "技能图标未收录", 404)
        return public_png(path, request)

    @application.get("/api/pets/{pet_id}/image")
    async def pet_image(pet_id: str, request: Request):
        path = catalog.image_path(pet_id)
        if path is None:
            raise ApiError("IMAGE_NOT_FOUND", "头像未收录", 404)
        return public_png(path, request)

    @application.get("/api/items/{item_id}/image")
    async def item_image(item_id: str):
        path = catalog.items.image_path(item_id)
        if path is None:
            raise ApiError("IMAGE_NOT_FOUND", "物品图标未收录", 404)
        return FileResponse(path, media_type="image/png")

    @application.get("/api/market/listings", response_model=MarketSnapshot)
    async def listings(request: Request, query: str | None = None, category: Category = "all", pet_ids: str | None = None):
        session = require_session(sessions, request, connected=True)
        ids = {value.strip() for value in pet_ids.split(",") if value.strip()} if pet_ids is not None else None
        return market.snapshot(session, query, category, ids)

    @application.post("/api/market/refresh", response_model=MarketSnapshot)
    async def refresh(body: RefreshRequest, request: Request):
        session = require_session(sessions, request, connected=True)
        async with session.lock:
            require_session(sessions, request, connected=True)
            snapshot = market.start(session, body.category, query=body.query,
                                    pet_ids={value.strip() for value in body.pet_ids if value.strip()} or None,
                                    limit=body.limit)
            state = session.market
        if body.limit is None:
            return snapshot
        assert state is not None
        return await market.result(session, state, timeout=market_timeout)

    @application.get("/api/rush-purchase", response_model=RushPurchaseState)
    async def rush_state(request: Request):
        session = require_session(sessions, request, connected=True)
        return rush.snapshot(session)

    @application.post("/api/rush-purchase/start", response_model=RushPurchaseState)
    async def start_rush(body: RushStartRequest, request: Request):
        session = require_session(sessions, request, connected=True)
        async with session.lock:
            require_session(sessions, request, connected=True)
            return rush.start(session, body)

    @application.post("/api/rush-purchase/stop", response_model=RushPurchaseState)
    async def stop_rush(body: RushStopRequest, request: Request):
        session = require_session(sessions, request, connected=True)
        async with session.lock:
            require_session(sessions, request, connected=True)
            return rush.stop(session, body.job_id)

    return application


app = create_app()
