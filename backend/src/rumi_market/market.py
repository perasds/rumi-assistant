import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .catalog import Catalog
from .errors import ApiError, auth_required, provider_error
from .models import Category, Listing, MarketSnapshot, NameCoverage
from .providers import ProviderListing
from .sessions import Session, SessionStore


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RefreshState:
    snapshot: MarketSnapshot
    limit: int | None = None
    query: str = ""
    filter_pet_ids: frozenset[str] | None = None
    matching_pet_ids: frozenset[str] | None = None
    task: asyncio.Task | None = None
    stopped: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    raw_ids: set[str] = field(default_factory=set, repr=False)
    raw_by_listing_key: dict[str, str] = field(default_factory=dict, repr=False)

    def stop(self) -> None:
        self.stopped = True
        self.done.set()


class MarketService:
    def __init__(self, catalog: Catalog, sessions: SessionStore):
        self.catalog = catalog
        self.sessions = sessions

    def start(self, session: Session, category: Category, *, query: str = "",
              pet_ids: set[str] | None = None, limit: int | None = None) -> MarketSnapshot:
        if session.market:
            session.market.stop()
            if session.market.task:
                session.market.task.cancel()
            session.market.raw_ids.clear()
            session.market.raw_by_listing_key.clear()
        now = utc_now()
        ids = frozenset(pet_ids) if pet_ids else None
        matching_ids = self.catalog.matching_ids(query) if query.strip() else None
        if ids is not None:
            matching_ids = ids if matching_ids is None else ids.intersection(matching_ids)
        state = RefreshState(MarketSnapshot(refresh_id=secrets.token_urlsafe(16),
                                            scope="top10" if limit is not None else "all",
                                            status="loading", started_at=now, updated_at=now),
                             limit=limit, query=query.strip().casefold(), filter_pet_ids=ids,
                             matching_pet_ids=frozenset(matching_ids) if matching_ids is not None else None)
        session.market = state
        state.task = asyncio.create_task(self._load(session, state, category))
        session.tasks.add(state.task)
        state.task.add_done_callback(session.tasks.discard)
        return state.snapshot.model_copy(deep=True)

    async def result(self, session: Session, state: RefreshState, *, timeout: float) -> MarketSnapshot:
        try:
            await asyncio.wait_for(state.done.wait(), timeout)
        except TimeoutError:
            self._require_result(session, state)
            self._fail(state, "商品查询超时，请重新搜索")
            state.stop()
            if state.task:
                state.task.cancel()
            raise ApiError("MARKET_QUERY_TIMEOUT", "商品查询超时，请重新搜索", 504, True) from None
        except asyncio.CancelledError:
            if self._current(session, state):
                self._fail(state, "商品查询已取消，请重新搜索")
            state.stop()
            if state.task:
                state.task.cancel()
            raise
        self._require_result(session, state)
        if state.snapshot.status == "loading":
            raise ApiError("MARKET_QUERY_CANCELLED", "商品查询已取消，请重新搜索", 409, True)
        return self.snapshot(session)

    @staticmethod
    def _require_result(session: Session, state: RefreshState) -> None:
        if not session.active or not session.view.authenticated:
            raise auth_required()
        if session.market is not state or state.stopped:
            raise ApiError("MARKET_QUERY_REPLACED", "本次查询已被新的搜索替换", 409)

    @staticmethod
    def _current(session: Session, state: RefreshState) -> bool:
        return session.active and session.view.authenticated and session.market is state and not state.stopped

    def _listing(self, row: ProviderListing, observed_at: datetime) -> Listing:
        pet = self.catalog.pet(row.pet_id) if row.pet_id is not None else None
        item = self.catalog.items.lookup(row.item_id) if pet is None and row.item_id is not None else None
        name = pet.name if pet else item.name if item else row.item_name
        image_url = pet.image_url if pet else (
            f"/api/items/{item.item_id}/image" if item and self.catalog.items.image_path(item.item_id) else None)
        return Listing(listing_key=secrets.token_urlsafe(16), category=row.category,
                       pet_id=row.pet_id, name=name,
                       display_name=pet.display_name if pet else name or "未识别物品",
                       image_url=image_url,
                       direct_buy_price=row.direct_buy_price,
                       expires_at=row.expires_at, observed_at=observed_at)

    async def _load(self, session: Session, state: RefreshState, category: Category) -> None:
        cursor = None
        seen_cursors: set[bytes | str] = set()
        try:
            if state.limit is not None and state.matching_pet_ids == frozenset():
                state.snapshot.status = "complete"
                state.snapshot.complete = True
                return
            while self._current(session, state):
                async with asyncio.timeout(30):
                    try:
                        page = (await session.provider.fetch_ranked_color_page(cursor)
                                if state.limit is not None
                                else await session.provider.fetch_page(category, cursor))
                    except ApiError as error:
                        raise provider_error(error) from None
                if not self._current(session, state):
                    return
                observed_at = utc_now()
                for row in page.items:
                    if not row.raw_id:
                        raise ApiError("INVALID_MARKET_PAGE", "商品分页数据不合法")
                    if state.limit is not None:
                        if row.category != "color":
                            raise ApiError("INVALID_MARKET_PAGE", "商品分页数据不合法")
                        if state.matching_pet_ids is not None and row.pet_id not in state.matching_pet_ids:
                            continue
                    if row.raw_id not in state.raw_ids:
                        item = self._listing(row, observed_at)
                        state.raw_ids.add(row.raw_id)
                        state.raw_by_listing_key[item.listing_key] = row.raw_id
                        state.snapshot.items.append(item)
                        if state.limit is not None and len(state.snapshot.items) >= state.limit:
                            state.snapshot.updated_at = observed_at
                            state.snapshot.status = "complete"
                            state.snapshot.complete = True
                            return
                state.snapshot.updated_at = observed_at
                if state.limit is None and page.total_count is not None:
                    if page.total_count < 0:
                        raise ApiError("INVALID_MARKET_PAGE", "商品分页数据不合法")
                    state.snapshot.total_count = page.total_count
                if page.complete:
                    state.snapshot.status = "complete"
                    state.snapshot.complete = True
                    return
                if page.next_cursor is None:
                    raise ApiError("MISSING_MARKET_CURSOR", "分页缺少后续游标，本轮未完成")
                if page.next_cursor in seen_cursors:
                    raise ApiError("REPEATED_MARKET_CURSOR", "服务端重复分页游标，本轮未完成")
                seen_cursors.add(page.next_cursor)
                cursor = page.next_cursor
                # Permit cancellation and HTTP polling even for immediate providers.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            return
        except ApiError as error:
            if self._current(session, state):
                if error.status_code == 401:
                    await self.sessions.expire(session)
                else:
                    self._fail(state, error.message)
        except Exception:
            if self._current(session, state):
                self._fail(state, "商品查询暂不可用，本轮未完成，请重新刷新")
        finally:
            state.done.set()

    @staticmethod
    def _fail(state: RefreshState, message: str) -> None:
        state.snapshot.status = "partial" if state.snapshot.items else "failed"
        state.snapshot.complete = False
        state.snapshot.message = message
        state.snapshot.updated_at = utc_now()

    def snapshot(self, session: Session, query: str | None = None, category: Category = "all",
                 pet_ids: set[str] | None = None) -> MarketSnapshot:
        snapshot = session.market.snapshot if session.market else MarketSnapshot()
        if session.market and session.market.limit is not None:
            state = session.market
            ids = frozenset(pet_ids) if pet_ids else None
            if (category not in ("all", "color") or ((query is not None or pet_ids is not None)
                    and ((query or "").strip().casefold() != state.query or ids != state.filter_pet_ids))):
                raise ApiError("MARKET_FILTER_CHANGED", "筛选条件已变化，请重新刷新商品", 409)
            # The bounded refresh already applied its bound criteria before taking ten.
            query, pet_ids = "", None
        query = query or ""
        name_ids = self.catalog.matching_ids(query) if query.strip() else None
        items = [item for item in snapshot.items
                 if (category == "all" or item.category == category)
                 and (pet_ids is None or item.pet_id in pet_ids)
                 and (name_ids is None or item.pet_id in name_ids
                      or (item.pet_id is None and item.name
                          and query.strip().casefold() in item.name.casefold()))]
        observed_ids = {item.pet_id for item in snapshot.items if item.pet_id is not None}
        coverage = NameCoverage(total=len(observed_ids), named=sum(
            self.catalog.pet(pet_id).name_verified for pet_id in observed_ids))
        return snapshot.model_copy(update={"items": items, "loaded_count": len(snapshot.items),
                                           "matched_count": len(items), "name_coverage": coverage})
