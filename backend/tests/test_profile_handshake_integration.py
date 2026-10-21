import asyncio
import json

from test_game_client import MemoryTransport
from test_game_handshake import complete_server, profile
from rumi_market.game.handshake import GameHandshake


class Collector:
    def __init__(self, *, fail=False):
        self.commands = []
        self.handshake_fields = {}
        self.finished_role = None
        self.closed = False
        self.archive_status = "pending"
        self.fail = fail

    def observe(self, response):
        self.commands.append(response.command)

    def record_handshake(self, stage, message, allowed_fields):
        self.handshake_fields[stage] = tuple(allowed_fields)

    def finalize(self, result):
        if self.fail:
            raise OSError("synthetic-private-filesystem-error")
        self.finished_role = result.role_id
        self.archive_status = "saved"

    def close(self):
        self.closed = True


async def test_handshake_collects_safe_fields_and_finalizes_after_login_end():
    collector = Collector()
    transport = MemoryTransport()
    handshake = GameHandshake(transport, profile_collector=collector)
    server = asyncio.create_task(complete_server(transport))
    try:
        result = await handshake.start(profile())
        await server
        assert collector.finished_role == result.role_id
        assert set(collector.commands) == {2902, 2912, 2913}
        fields = {field for stage in collector.handshake_fields.values() for field in stage}
        assert {"role_id", "zone_id", "zone_name", "server_time_ms"} <= fields
        assert not {"session_info", "reconnect_token", "cd_key"}.intersection(fields)
        assert handshake.profile_archive_status == "saved"
    finally:
        await handshake.close()
    assert collector.closed


async def test_optional_archive_failure_does_not_break_login():
    collector = Collector(fail=True)
    transport = MemoryTransport()
    handshake = GameHandshake(transport, profile_collector=collector)
    server = asyncio.create_task(complete_server(transport))
    try:
        await handshake.start(profile())
        await server
        assert handshake.can_query
        assert handshake.profile_archive_status == "failed"
    finally:
        await handshake.close()


async def test_profile_notifications_reach_private_file_through_normal_runtime(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from test_game_profile_sync import message
    from test_game_runtime import setup, PRIVATE, TOKEN, CODEC
    from rumi_market.profile_archive import AccountProfileArchive

    archive_root = tmp_path
    runtime, game, _, _, _, _ = setup(
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        profile_archive=AccountProfileArchive(archive_root))

    async def serve():
        login = await game.next_request()
        game.respond(2902, login.ReqCmdSeq, CODEC.encode("SNKProto.Cmd_Net_LumiLogin_Ack", {
            "role_id": 123, "reconnect_token": "synthetic-reconnect-secret"}))
        enter = await game.next_request()
        game.respond(2912, enter.ReqCmdSeq, CODEC.encode("SNKProto.Cmd_Role_EnterGame_Ack", {
            "zone_id": 7, "zone_name": "synthetic-zone", "server_time_ms": 1700000000123}))
        game.respond(2931, 0, message("Cmd_Notify_RoleBase", role_id=123,
                                    name="合成归档角色", level=19).SerializeToString(), notify=1)
        game.respond(2941, 0, message("Cmd_Notify_ItemAll", item_list=[
            {"item_id": 10001, "count": 42}]).SerializeToString(), notify=2)
        game.respond(5432, 0, message("Cmd_Notify_Guild_Role_Data", data={
            "gid": 2**63 + 15}).SerializeToString(), notify=3)
        game.respond(2913, 0, CODEC.encode("SNKProto.Cmd_Notify_LoginEnd", {
            "cd_key": "synthetic-activation-secret"}), notify=4)

    server = asyncio.create_task(serve())
    try:
        await runtime.start("synthetic-account", PRIVATE)
        await server
        assert runtime.can_query and runtime.profile_archive_status == "saved"
        assert runtime.gold_balance == 42
        tracker = runtime.account_state
        revision = tracker.gold_revision
        snapshots = list(archive_root.glob("*/*.json"))
        assert len(snapshots) == 1
        text = snapshots[0].read_text(encoding="utf-8")
        document = json.loads(text)
        assert document["source"] == "game_server"
        assert document["derived_candidates"]["raw_item_10001_sum"] == "42"
        assert document["derived_candidates"]["self_guild_id"] == str(2**63 + 15)
        assert {2931, 2941, 5432} <= {row["command"] for row in document["messages"]
                                                     if row["status"] == "decoded"}
        assert "合成归档角色" in text
        assert all(secret not in text for secret in (
            PRIVATE, TOKEN, "synthetic-reconnect-secret", "synthetic-activation-secret"))
        assert not {"session_info", "reconnect_token", "cd_key"} & document["handshake"]["login_ack"].keys()
        changed = message("CmdItemReal", item_id=10001, count=31)
        game.respond(2942, 0, message("Cmd_Notify_ItemChange", change_list=[
            {"change_type": 3, "data": changed.SerializeToString()}]).SerializeToString(), notify=5)
        assert await tracker.wait_for_gold_balance(31, revision, timeout=1)
        assert runtime.gold_balance == 31
        # Live state continues after the immutable login archive has finalized.
        assert snapshots[0].read_text(encoding="utf-8") == text
    finally:
        server.cancel()
        await runtime.close()
    assert runtime.gold_balance is None and tracker.gold_balance is None


class AccountStateObserver:
    def __init__(self, *, fail=False):
        self.commands = []
        self.invalidated = False
        self.closed = False
        self.fail = fail

    def observe(self, response):
        self.commands.append(response.command)
        if self.fail:
            raise ValueError("synthetic-private-state-error")

    def invalidate(self):
        self.invalidated = True

    def close(self):
        self.closed = True


async def test_failing_archive_observer_does_not_stop_account_state_or_login():
    class BrokenArchive(Collector):
        def observe(self, response):
            raise ValueError("synthetic-private-archive-error")
    collector, account_state = BrokenArchive(), AccountStateObserver()
    transport = MemoryTransport()
    handshake = GameHandshake(transport, profile_collector=collector, account_state=account_state)
    server = asyncio.create_task(complete_server(transport))
    try:
        await handshake.start(profile())
        await server
        assert handshake.can_query and account_state.commands == [2902, 2913, 2912]
        assert handshake.profile_archive_status == "failed"
        assert not account_state.invalidated
    finally:
        await handshake.close()
    assert account_state.closed and collector.closed


async def test_failing_account_state_observer_does_not_stop_profile_archive_or_login():
    collector, account_state = Collector(), AccountStateObserver(fail=True)
    transport = MemoryTransport()
    handshake = GameHandshake(transport, profile_collector=collector, account_state=account_state)
    server = asyncio.create_task(complete_server(transport))
    try:
        await handshake.start(profile())
        await server
        assert handshake.can_query and collector.finished_role == 123
        assert account_state.invalidated
        assert handshake.profile_archive_status == "saved"
    finally:
        await handshake.close()
    assert account_state.closed and collector.closed
