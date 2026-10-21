"""Inspection HTTP permissions/lifecycle using synthetic real-config home rows."""
import asyncio
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from rumi_market.app import create_app
from rumi_market.auth import COOKIE_NAME
from rumi_market.sessions import Session
from test_home_management_api import snapshot as home_snapshot
from test_order_service import Provider, setup


STATE = "/api/home/orders/state"
START = "/api/home/orders/inspection/start"
ORDER_START = "/api/home/orders/task/start"
STOP = "/api/home/orders/task/stop"
RESOLVE = "/api/home/orders/task/resolve"


def error_code(response):
    return response.json()["error"]["code"]


class InspectionProvider(Provider):
    def __init__(self, *, materials=True):
        super().__init__()
        # Actual public 10101/level1 recipe1: stardust1 + wood5 -> Lumi card1.
        # The application keeps its real HomeWorkConfig and recipe catalog.
        self.raw["inventory"]["items"] = {"1": 0, "20000": 10 if materials else 0,
                                             "20001": 50 if materials else 0, "10031": 4}
        self.raw["home"] = home_snapshot()
        building = self.raw["home"]["architectures"][0]
        building.update(capacity_remaining=20, store_items=[], store_extra_items=[])
        building["work_old"].update(recipe_id=0, recipe_intend_times=0, recipe_complete_times=0,
            lumi_work_energy=100000, lumi_work_speed=1000, recipe_work_amount=0,
            recipe_update_time=1000, recipe_state=0)
        self.raw["home"]["workers"][0]["work_energy"] = 100000
        self.home_writes = []
        self.started = asyncio.Event()

    async def home_snapshot(self):
        return deepcopy(self.raw["home"])

    async def apply_home_change(self, change, *, expected):
        self.home_writes.append(deepcopy(change))
        return {"status": "uncertain", "message": "synthetic manual result"}

    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        if stop_requested():
            return {"status": "rejected", "rewards": []}
        assert operation["action"] in ("produce", "claim"), "Inspection must never submit or refresh orders"
        if operation["action"] == "claim":
            assert operation == {"action": "claim", "building_uid": "123", "building_id": "10101"}
            self.writes.append(deepcopy(operation))
            self.raw["home"]["architectures"][0]["store_items"] = []
            self.raw["home"]["sequence"] += 1
            self.raw["inventory"]["items"]["10031"] += 1
            self.raw["inventory"]["sequence"] += 1
            return {"status": "applied", "rewards": [{"item_id": "10031", "count": 1}]}
        assert operation == {"action": "produce", "building_uid": "123", "building_id": "10101",
                             "recipe_id": "1", "recipe_times": 1}
        self.writes.append(deepcopy(operation))
        self.started.set()
        if self.outcome == "applied":
            building = self.raw["home"]["architectures"][0]
            building["work_old"].update(recipe_id=1, recipe_intend_times=1, recipe_state=1)
            self.raw["home"]["sequence"] += 1
            self.raw["inventory"]["items"]["20000"] -= 1
            self.raw["inventory"]["items"]["20001"] -= 5
            self.raw["inventory"]["sequence"] += 1
        return {"status": self.outcome, "rewards": []}

    def produced(self):
        building = self.raw["home"]["architectures"][0]
        building["store_items"] = [{"item_id": "10031", "count": 1}]
        building["work_old"].update(recipe_complete_times=1, recipe_state=0)
        self.raw["home"]["sequence"] += 1


def client_with_inspection(*, materials=True):
    provider = InspectionProvider(materials=materials)
    _, session, _ = setup(provider)
    app = create_app()
    # Explicitly enable the synthetic card-table fixture for lifecycle tests.
    app.state.orders.auto_production_exclusions = frozenset()
    app.state.orders.sync_timeout = .02
    app.state.sessions.sessions[session.id] = session
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, session.id)
    return client, provider, session


def body(client, request_id="inspection-request-0001"):
    response = client.get(STATE)
    assert response.status_code == 200
    return {"request_id": request_id, "expected_revision": response.json()["revision"]}


async def finished(session):
    assert session.order_task is not None
    await asyncio.wait_for(session.order_task.task, 1)


def other_browser(client, session):
    provider = InspectionProvider(materials=False)
    other = Session("inspection-second-browser", provider, view=session.view)
    client.app.state.sessions.sessions[other.id] = other
    client.cookies.set(COOKIE_NAME, other.id)
    return other, provider


def test_inspection_start_requires_login_and_same_origin():
    with TestClient(create_app()) as guest:
        response = guest.post(START, json={"request_id": "inspection-request-0001", "expected_revision": "a" * 64})
        assert response.status_code == 401
    client, provider, _ = client_with_inspection()
    with client:
        response = client.post(START, json=body(client), headers={"origin": "https://other.invalid"})
        assert response.status_code == 403
        assert error_code(response) == "ORIGIN_REJECTED"
        assert provider.writes == []


def test_application_defaults_skip_card_table_but_keep_manual_home_configuration():
    provider = InspectionProvider()
    _, session, _ = setup(provider)
    app = create_app()
    app.state.sessions.sessions[session.id] = session
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, session.id)
        state = client.get(STATE).json()
        row = state['work_inspection']['buildings'][0]
        assert row['building_id'] == '10101'
        assert row['candidate'] is None and row['message'] == '默认不安排自动生产'
        home = client.get('/api/home/state').json()
        assert home['buildings'][0]['editable'] is True
        response = client.post(START, json=body(client))
        assert response.status_code == 200 and response.json()['task']['running'] is True
        assert client.post(STOP).status_code == 200
        assert provider.writes == [] and provider.home_writes == []


def test_state_refresh_settings_and_preview_are_read_only_and_never_start_inspection():
    client, provider, session = client_with_inspection()
    with client:
        for _ in range(3):
            response = client.get(STATE)
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            state = response.json()
            inspection = state["work_inspection"]
            assert inspection["ready"] is True
            assert inspection["reason"] is None and isinstance(inspection["buildings"], list)
            assert "synthetic-private-value" not in response.text
            assert "role_key" not in response.text
        assert client.put("/api/home/orders/settings", json=state["settings"]).status_code == 200
        assert client.post("/api/home/orders/plan", json={
            "expected_revision": state["revision"], "settings": state["settings"]}).status_code == 200
        assert provider.writes == [] and provider.home_writes == []
        assert session.order_task is None and not session.tasks


def test_inspection_api_reuses_public_pet_catalog_and_recipe_names_without_writes():
    client, provider, session = client_with_inspection()
    provider.raw['home']['architectures'][0]['work_old']['recipe_id'] = 1
    with client:
        response = client.get(STATE)
        assert response.status_code == 200
        row = response.json()['work_inspection']['buildings'][0]
        pet = client.app.state.catalog.pet('102601')
        assert pet.name == '筋肉狗'
        assert row['worker_uid'] == '9007199254740993'
        assert row['worker_name'] == pet.name
        assert row['worker_image_url'] == pet.image_url
        assert row['current_recipe_products'] == [
            {'item_id': '10031', 'name': '噜咪卡', 'count': '1'}]
        assert provider.writes == [] and provider.home_writes == []
        assert session.order_task is None


def test_inspection_resolves_read_only_amethyst_recipe_without_enabling_production():
    client, provider, _ = client_with_inspection()
    building = provider.raw['home']['architectures'][0]
    building.update(id=10114, cur_recipe={'recipe_id': 24})
    building['work_old']['recipe_id'] = 24
    with client:
        response = client.get(STATE)
        assert response.status_code == 200
        row = response.json()['work_inspection']['buildings'][0]
        assert row['name'] == '紫晶矿' and row['status'] == 'blocked'
        assert row['current_recipe_id'] == '24'
        assert row['current_recipe_products'] == [
            {'item_id': '20004', 'name': '紫晶砂', 'count': '1'}]
        assert row['candidate'] is None
        assert provider.writes == [] and provider.home_writes == []


@pytest.mark.parametrize("change", [
    {"kind": "inspection"}, {"kind": "orders"}, {"recipe_times": 100},
    {"allow_production": True}, {"request_id": True}, {"expected_revision": 1},
    {"request_id": "short"}, {"expected_revision": "invalid"},
    {"account_password": "synthetic-sensitive"},
])
def test_inspection_start_body_is_strict_without_echoing_private_input(change):
    client, provider, session = client_with_inspection()
    with client:
        request = body(client) | change
        response = client.post(START, json=request)
        assert response.status_code == 422
        assert "synthetic-sensitive" not in response.text
        assert provider.writes == [] and session.order_task is None


@pytest.mark.parametrize("first_path,second_path,kind", [
    (START, ORDER_START, "inspection"), (ORDER_START, START, "orders"),
])
def test_request_id_replay_is_idempotent_but_changing_task_kind_conflicts(first_path, second_path, kind):
    client, provider, session = client_with_inspection(materials=False)
    with client:
        request = body(client)
        first = client.post(first_path, json=request)
        assert first.status_code == 200
        run = session.order_task
        assert first.json()["task"]["kind"] == kind
        assert client.post(first_path, json=request).status_code == 200
        assert session.order_task is run
        conflict = client.post(second_path, json=request)
        assert conflict.status_code == 409 and error_code(conflict) == "ORDER_REQUEST_CONFLICT"
        assert provider.writes == []
        assert client.post(STOP, json={}).status_code == 200
        client.portal.call(finished, session)


@pytest.mark.parametrize("first_path,second_path", [(START, ORDER_START), (ORDER_START, START), (START, START)])
def test_same_real_role_is_exclusive_across_browser_sessions(first_path, second_path):
    client, provider, session = client_with_inspection(materials=False)
    with client:
        assert client.post(first_path, json=body(client)).status_code == 200
        _, other = other_browser(client, session)
        response = client.post(second_path, json=body(client, "inspection-request-0002"))
        assert response.status_code == 409 and error_code(response) == "ORDER_ROLE_BUSY"
        assert provider.writes == other.writes == []
        client.cookies.set(COOKIE_NAME, session.id)
        assert client.post(STOP, json={}).status_code == 200
        client.portal.call(finished, session)


def test_manual_home_lease_blocks_inspection_in_another_browser():
    client, provider, session = client_with_inspection(materials=False)
    with client:
        lease = client.app.state.orders.home_lease(session)
        client.portal.call(lease.__aenter__)
        try:
            _, other = other_browser(client, session)
            response = client.post(START, json=body(client))
            assert response.status_code == 409 and error_code(response) == "ORDER_ROLE_BUSY"
            assert provider.writes == other.writes == []
        finally:
            client.portal.call(lease.__aexit__, None, None, None)


def test_running_inspection_blocks_manual_home_apply_without_stopping_game_work():
    client, provider, session = client_with_inspection(materials=False)
    with client:
        assert client.post(START, json=body(client)).status_code == 200
        other_browser(client, session)
        home = client.get("/api/home/state")
        assert home.status_code == 200
        response = client.put("/api/home/settings", json={"request_id": "inspection-manual-0001",
            "expected_revision": home.json()["revision"],
            "changes": [{"building_uid": "123", "recipe_id": "1", "production_count": 1}]})
        assert response.status_code == 409 and error_code(response) == "ORDER_ROLE_BUSY"
        assert provider.home_writes == []
        client.cookies.set(COOKIE_NAME, session.id)
        assert client.post(STOP, json={}).status_code == 200
        client.portal.call(finished, session)


def test_stop_without_an_inflight_action_releases_role_and_refresh_never_restarts():
    client, provider, session = client_with_inspection(materials=False)
    with client:
        started = client.post(START, json=body(client))
        assert started.status_code == 200
        assert started.json()["task"]["kind"] == "inspection"
        assert int(started.json()["task"]["inspected_actions"]) == 0
        assert client.post(STOP, json={}).status_code == 200
        client.portal.call(finished, session)
        for _ in range(3):
            state = client.get(STATE).json()
            assert state["task"]["status"] == "stopped" and not state["task"]["running"]
        assert not client.app.state.orders.roles and provider.writes == []


def test_unknown_inspection_result_keeps_shared_lock_until_explicit_current_revision_resolution():
    client, provider, session = client_with_inspection()
    provider.outcome = "uncertain"
    with client:
        started = client.post(START, json=body(client))
        assert started.status_code == 200
        client.portal.call(finished, session)
        assert len(provider.writes) == 1 and provider.writes[0]["recipe_times"] == 1
        state = client.get(STATE).json()
        assert state["task"]["kind"] == "inspection" and state["task"]["status"] == "uncertain"
        _, other = other_browser(client, session)
        for path in (START, ORDER_START):
            response = client.post(path, json=body(client, "inspection-request-0002"))
            assert response.status_code == 409 and error_code(response) == "ORDER_ROLE_BUSY"
        assert other.writes == []
        assert client.post(STOP, json={}).status_code == 200
        assert client.get(STATE).json()["task"]["status"] == "uncertain"
        revision = client.get(STATE).json()["revision"]
        assert client.post(RESOLVE, json={"expected_revision": revision, "acknowledged": False}).status_code == 422
        resolved = client.post(RESOLVE, json={"expected_revision": revision, "acknowledged": True})
        assert resolved.status_code == 200 and resolved.json()["task"]["status"] == "stopped"
        assert not client.app.state.orders.roles
        for _ in range(3):
            assert client.get(STATE).status_code == 200
        assert len(provider.writes) == 1 and other.writes == []


@pytest.mark.parametrize("section,field,value", [
    ("home", "roster_ready", False), ("home", "dispatch_ready", False),
    ("home", "valid", False), ("inventory", "valid", False),
    (None, "role_key", ("7", None)), (None, "server_time_seconds", None),
])
def test_inspection_requires_current_complete_worker_resource_identity_and_clock(section, field, value):
    client, provider, session = client_with_inspection()
    target = provider.raw if section is None else provider.raw[section]
    target[field] = value
    with client:
        state = client.get(STATE)
        assert state.status_code == 200
        inspection = state.json()["work_inspection"]
        assert inspection["ready"] is False and inspection["reason"]
        response = client.post(START, json={"request_id": "inspection-unready-0001",
                                           "expected_revision": state.json()["revision"]})
        assert response.status_code == 409 and error_code(response) == "ORDER_STATE_UNAVAILABLE"
        assert provider.writes == [] and session.order_task is None


def test_saved_unverified_order_refresh_does_not_prevent_independent_inspection():
    client, provider, session = client_with_inspection(materials=False)
    with client:
        settings = client.get(STATE).json()["settings"] | {"allow_refresh": True, "allow_production": False}
        assert client.put("/api/home/orders/settings", json=settings).status_code == 200
        order = client.post(ORDER_START, json=body(client, "inspection-order-rejected-01"))
        assert order.status_code == 409 and error_code(order) == "ORDER_REFRESH_UNVERIFIED"
        inspection = client.post(START, json=body(client))
        assert inspection.status_code == 200 and inspection.json()["task"]["kind"] == "inspection"
        assert session.order_task.settings.allow_refresh is True
        assert client.post(STOP, json={}).status_code == 200
        client.portal.call(finished, session)
        assert provider.writes == []


def test_uncertain_manual_home_result_blocks_inspection_from_another_browser():
    client, provider, session = client_with_inspection(materials=False)
    with client:
        home = client.get("/api/home/state")
        assert home.status_code == 200
        applied = client.put("/api/home/settings", json={"request_id": "inspection-manual-unknown-01",
            "expected_revision": home.json()["revision"],
            "changes": [{"building_uid": "123", "recipe_id": "1", "production_count": 1}]})
        assert applied.status_code == 200 and applied.json()["last_apply"]["status"] == "uncertain"
        assert len(provider.home_writes) == 1 and session.home_write_uncertain
        _, other = other_browser(client, session)
        response = client.post(START, json=body(client))
        assert response.status_code == 409 and error_code(response) == "ORDER_ROLE_BUSY"
        assert other.writes == []


def test_inspection_can_finish_its_one_batch_without_waiting_for_order_notifications():
    client, provider, session = client_with_inspection()
    provider.raw["orders"]["valid"] = False
    with client:
        initial = client.get(STATE).json()
        assert initial["work_inspection"]["ready"] is True and initial["execution_ready"] is False
        started = client.post(START, json=body(client))
        assert started.status_code == 200
        client.portal.call(provider.started.wait)
        client.portal.call(provider.produced)

        async def claimed_and_settled():
            async with asyncio.timeout(2):
                while len(provider.writes) < 2 or session.order_task.in_flight:
                    assert not session.order_task.task.done(), "Inspection incorrectly depends on order synchronization"
                    await asyncio.sleep(.01)

        client.portal.call(claimed_and_settled)
        assert [operation["action"] for operation in provider.writes] == ["produce", "claim"]
        assert provider.raw["inventory"]["items"]["10031"] == 5
        assert provider.raw["inventory"]["items"]["20000"] == 9
        assert provider.raw["inventory"]["items"]["20001"] == 45
        assert session.order_task.inspected_actions == 1
        assert client.post(STOP, json={}).status_code == 200
        client.portal.call(finished, session)
        assert client.get(STATE).json()["task"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_native_claim_works_without_order_baseline_but_completion_still_requires_it():
    from rumi_market.order_service import order_revision
    from test_order_operations import setup as native_setup
    operations, native, raw, stop, _ = native_setup()
    raw["orders"]["valid"] = False
    revision = order_revision(raw)
    complete = await operations.apply({"action": "complete", "slot_id": "1"},
                                     expected_revision=revision, stop_requested=stop.is_set)
    assert complete["status"] == "rejected" and native.calls == []
    claim = await operations.apply({"action": "claim", "building_uid": "123", "building_id": "10101"},
                                  expected_revision=revision, stop_requested=stop.is_set)
    assert claim["status"] == "applied"
    assert native.calls == [(3025, 3026, {"archi_uid": 123, "archi_id": 10101})]


@pytest.mark.asyncio
async def test_explicit_provider_reconciliation_of_inspection_requires_home_and_inventory_not_orders():
    from types import SimpleNamespace
    from rumi_market.errors import ApiError
    from rumi_market.game_provider import IndependentGameProvider
    from rumi_market.order_service import order_revision
    from test_order_operations import snapshot
    raw = snapshot()
    raw["orders"]["valid"] = False
    provider = IndependentGameProvider(SimpleNamespace(), None, login_verified=True)
    provider._order_snapshot_now = lambda: deepcopy(raw)
    provider._order_operations = object()
    provider._order_production_operations = object()
    provider._home_operations = object()
    with pytest.raises(ApiError, match="ORDER_STATE_CHANGED"):
        await provider.reconcile_order_operations(expected_revision=order_revision(raw))
    assert provider._order_operations is not None and provider._order_production_operations is not None
    await provider.reconcile_order_operations(expected_revision=order_revision(raw), require_orders=False)
    assert provider._order_operations is None and provider._order_production_operations is None
    assert provider._home_operations is None


@pytest.mark.asyncio
@pytest.mark.parametrize("already_sent", [False, True])
async def test_expiry_during_manual_http_apply_retains_only_the_sent_unknown_role_lock(already_sent):
    import httpx

    class ExpiringHomeProvider(InspectionProvider):
        def __init__(self):
            super().__init__(materials=False)
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.exited = asyncio.Event()

        async def apply_home_change(self, change, *, expected):
            self.home_writes.append(deepcopy(change))  # Synthetic native-send boundary.
            self.entered.set()
            try:
                await self.release.wait()
                raise asyncio.CancelledError()
            finally:
                self.exited.set()

        async def close(self):
            self.release.set()
            if self.entered.is_set():
                await self.exited.wait()

    provider = ExpiringHomeProvider()
    _, session, _ = setup(provider)
    app = create_app()
    app.state.sessions.sessions[session.id] = session
    role = provider.order_identity
    task = None
    held_session_lock = False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                cookies={COOKIE_NAME: session.id}) as client:
        try:
            home = (await client.get("/api/home/state")).json()
            request_id = "inspection-manual-expiry-01"
            request = {"request_id": request_id, "expected_revision": home["revision"],
                       "changes": [{"building_uid": "123", "recipe_id": "1", "production_count": 1}]}
            if not already_sent:
                await session.lock.acquire()
                held_session_lock = True
            task = asyncio.create_task(client.put("/api/home/settings", json=request))
            async with asyncio.timeout(1):
                if already_sent:
                    await provider.entered.wait()
                    record = session.home_requests[request_id]
                else:
                    while role not in app.state.orders.home_busy:
                        await asyncio.sleep(0)
                    assert provider.home_writes == [] and request_id not in session.home_requests
            await app.state.sessions.expire(session)
            if held_session_lock:
                session.lock.release()
                held_session_lock = False
            result = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1))[0]
            assert session.view.status == "expired" and not session.home_write_uncertain
            assert role not in app.state.orders.home_busy
            if already_sent:
                assert len(provider.home_writes) == 1
                assert record.result is not None and record.result.status == "uncertain"
                assert isinstance(result, BaseException) or result.status_code == 401
                assert role in app.state.orders.roles and app.state.orders.roles[role].uncertain
            else:
                assert result.status_code == 401 and provider.home_writes == []
                assert role not in app.state.orders.roles
        finally:
            if held_session_lock:
                session.lock.release()
            provider.release.set()
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await app.state.sessions.close()
