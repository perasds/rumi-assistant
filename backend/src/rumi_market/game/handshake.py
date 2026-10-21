"""Normal game login handshake; real query success is a separate verification."""

import asyncio
from dataclasses import dataclass, replace
from math import isfinite
from time import monotonic

from .client import GameClient
from .login import GameLoginProfile, build_enter_game_request, build_sdk_login_request
from .proto import GameProtoCodec
from .transport import GameTransport


LOGIN_ACK = 2902
ENTER_ACK = 2912
LOGIN_END = 2913
INITIAL_PET_SELECTION = 2934


class GameHandshakeError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class GameHandshakeResult:
    role_id: int | None
    zone_id: int | None
    zone_name: str
    server_time_ms: int | None
    needs_activation: bool
    needs_initial_pet_selection: bool


class GameHandshake:
    def __init__(self, transport: GameTransport, *, timeout: float = 15.0,
                 profile_collector=None, account_state=None, home_state=None,
                 inventory_state=None, order_state=None, home_capacity_state=None, season_state=None,
                 redbag_state=None):
        self._profile_collector = profile_collector
        self._account_state = account_state
        self._home_state = home_state
        self._season_state = season_state
        self._redbag_state = redbag_state
        self._order_consumers = [state for state in (inventory_state, order_state, home_capacity_state) if state is not None]
        self._profile_archive_failed = False
        self.client = GameClient(transport, timeout=timeout,
            notification_commands=frozenset({LOGIN_END, INITIAL_PET_SELECTION}),
            response_observer=self._observe_response if any(
                consumer is not None for consumer in (profile_collector, account_state, home_state, inventory_state, order_state, home_capacity_state, season_state, redbag_state)) else None)
        self._started = False
        self._result = None
        self._server_clock = None

    @property
    def profile_archive_status(self) -> str | None:
        if self._profile_archive_failed or self.client.observer_failed:
            return "failed"
        return self._profile_collector.archive_status if self._profile_collector is not None else None

    def _observe_response(self, response):
        # The immutable login archive and live account state are independent
        # consumers. A disk/schema failure in either must not hide the other.
        if self._redbag_state is not None:
            try:
                self._redbag_state.observe(response)
            except Exception:
                self._redbag_state.invalidate()
        if self._season_state is not None:
            try:
                self._season_state.observe(response)
            except Exception:
                self._season_state.clear()
        for state in self._order_consumers:
            try:
                state.observe(response)
            except Exception:
                try:
                    state.invalidate()
                except Exception:
                    pass
        if self._profile_collector is not None:
            try:
                self._profile_collector.observe(response)
            except Exception:
                self._profile_archive_failed = True
        if self._account_state is not None:
            try:
                self._account_state.observe(response)
            except Exception:
                try:
                    self._account_state.invalidate()
                except Exception:
                    pass
        if self._home_state is not None:
            try:
                self._home_state.observe(response)
            except Exception:
                try:
                    self._home_state.invalidate()
                except Exception:
                    pass

    def _record_profile_handshake(self, stage, message, allowed_fields):
        if self._profile_collector is not None:
            try:
                self._profile_collector.record_handshake(stage, message, allowed_fields)
            except Exception:
                self._profile_archive_failed = True

    @property
    def result(self) -> GameHandshakeResult | None:
        if (self._result is not None and not self._result.needs_initial_pet_selection
                and self.client.received_notification(INITIAL_PET_SELECTION)):
            self._result = replace(self._result, needs_initial_pet_selection=True)
        return self._result

    @property
    def can_query(self) -> bool:
        """Local gate for attempting a read, not proof of game access or eligibility."""
        result = self.result
        return (result is not None and not self.client.closed
                and not result.needs_activation and not result.needs_initial_pet_selection)

    @property
    def server_time_seconds(self) -> int | None:
        """ACK Unix milliseconds advanced by a local monotonic elapsed time.

        Monotonic time is project policy; the source client advances the same
        ACK value with elapsed UTC wall-clock milliseconds. Invalid or backwards
        local time invalidates this clock until a new independent login.
        """
        if self._server_clock is None or self.client.closed:
            return None
        server_ms, anchor, previous = self._server_clock
        now = monotonic()
        if type(now) not in (int, float) or not isfinite(now) or now < previous:
            self._server_clock = None
            return None
        elapsed_ms = int((now - anchor) * 1000)
        self._server_clock = (server_ms, anchor, now)
        return (server_ms + elapsed_ms) // 1000

    async def start(self, profile: GameLoginProfile) -> GameHandshakeResult:
        if self._started:
            raise GameHandshakeError("GAME_HANDSHAKE_ALREADY_STARTED")
        self._started = True
        try:
            login_request = build_sdk_login_request(profile)
            enter_request = build_enter_game_request(profile)
            await self.client.connect()
            codec = GameProtoCodec()
            login_response = await self.client.request(
                login_request.command_id, LOGIN_ACK, login_request.payload)
            login_ack = codec.decode("SNKProto.Cmd_Net_LumiLogin_Ack", login_response.data).message
            self._record_profile_handshake("login_ack", login_ack, ("role_id", "ban_expire_time"))
            enter_response = await self.client.request(
                enter_request.command_id, ENTER_ACK, enter_request.payload)
            enter_ack = codec.decode("SNKProto.Cmd_Role_EnterGame_Ack", enter_response.data).message
            self._record_profile_handshake("enter_ack", enter_ack,
                                           ("zone_id", "zone_name", "server_time_ms"))
            server_ms = enter_ack.server_time_ms if enter_ack.HasField("server_time_ms") else None
            anchor = monotonic()
            if (type(server_ms) is int and server_ms > 0
                    and type(anchor) in (int, float) and isfinite(anchor)):
                # Anchor before waiting for LoginEnd, which can arrive later.
                self._server_clock = (server_ms, anchor, anchor)
            end_response = await self.client.wait_notification(LOGIN_END)
            end = codec.decode("SNKProto.Cmd_Notify_LoginEnd", end_response.data).message
            self._record_profile_handshake("login_end", end, ("battle_id",))
            # Source handler checks the non-null 2934 object, even if its list is empty.
            # Only receipt is needed here; selection contents are not exposed or inferred.
            self._result = GameHandshakeResult(
                role_id=login_ack.role_id if login_ack.HasField("role_id") else None,
                zone_id=enter_ack.zone_id if enter_ack.HasField("zone_id") else None,
                zone_name=enter_ack.zone_name,
                server_time_ms=enter_ack.server_time_ms if enter_ack.HasField("server_time_ms") else None,
                needs_activation=end.use_cd_key and not end.cd_key,
                needs_initial_pet_selection=self.client.received_notification(INITIAL_PET_SELECTION),
            )
            if self._season_state is not None and self.can_query:
                self._season_state.mark_login_complete()
            if self._profile_collector is not None:
                try:
                    self._profile_collector.finalize(self.result)
                except Exception:
                    self._profile_archive_failed = True
            return self.result
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        season_state, self._season_state = self._season_state, None
        if season_state is not None:
            season_state.clear()
        consumers, self._order_consumers = self._order_consumers, []
        for state in consumers:
            state.close()
        self._result = None
        self._server_clock = None
        home_state, self._home_state = self._home_state, None
        if home_state is not None:
            try:
                home_state.close()
            except Exception:
                pass
        account_state, self._account_state = self._account_state, None
        if account_state is not None:
            try:
                account_state.close()
            except Exception:
                pass
        collector, self._profile_collector = self._profile_collector, None
        if collector is not None:
            try:
                collector.close()
            except Exception:
                self._profile_archive_failed = True
        await self.client.close()
