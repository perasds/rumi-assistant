"""Build the minimal season wire projection from reviewed public hot65 evidence.

Reads public schema/CIL evidence only. Does not load accounts, contact servers,
execute game code, or export assemblies. Omitted fields stay protobuf unknowns.
"""
import hashlib
import json
from pathlib import Path

from google.protobuf import descriptor_pb2

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "artifacts/rumi/market-season-analysis/market-season-runtime.public.json"
GAME_SHA = "ab029ae5433cbaa8b214a290387b40ae762a154cbc04096a26c886eba4eab4b3"
EVIDENCE_SHA = "c45e20cd6eb152a3c8fca3cfce90654dbd5c3139b8cc5bd3c3e137f1f864018e"
DESTINATION = Path(__file__).resolve().parents[1] / "backend/src/rumi_market/game"
FIELDS = {
    "SNKProto.CmdACTComm": {"system_open_id", "system_id", "begin_time", "end_time"},
    "SNKProto.CmdACTSeasonSystem": {"act_comm"},
    "SNKProto.CmdACTAll": {"season_list"},
    "SNKProto.Cmd_Notify_ACT_All": {"activity_info"},
    "SNKProto.Cmd_Notify_ACT_Change": {"activity_info"},
}


def main():
    evidence_bytes = EVIDENCE.read_bytes()
    if hashlib.sha256(evidence_bytes).hexdigest() != EVIDENCE_SHA:
        raise ValueError("Reviewed public season evidence differs")
    report = json.loads(evidence_bytes)
    if report["source_game_sha256"] != GAME_SHA:
        raise ValueError("Reviewed public hot65 source differs")
    bindings = {row["rpc_id"]: row["message_type"] for row in report["rpc_bindings"]}
    expected = {8001: "SNKProto.Cmd_Notify_ACT_All", 8002: "SNKProto.Cmd_Notify_ACT_Change"}
    if bindings != expected:
        raise ValueError("Reviewed season dispatch differs")
    schemas = report["season_schemas"] | report["root_season_fields"]
    result = descriptor_pb2.FileDescriptorSet()
    file = result.file.add(name="ProtoGame.proto", package="SNKProto", syntax="proto3")
    promoted = {}
    for owner, selected in FIELDS.items():
        message = file.message_type.add(name=owner.removeprefix("SNKProto."))
        fields = [f for f in schemas[owner]["fields"] if f["name"] in selected]
        if {f["name"] for f in fields} != selected:
            raise ValueError("Reviewed season field projection differs")
        promoted[owner] = fields
        for value in fields:
            field = message.field.add(name=value["name"], number=value["number"],
                                      type=value["type"], label=value["label"])
            if value["type_name"]:
                field.type_name = value["type_name"]
            if value["proto3_optional"]:
                field.proto3_optional = True
                field.oneof_index = len(message.oneof_decl)
                message.oneof_decl.add(name="_" + field.name)
    raw = result.SerializeToString(deterministic=True)
    descriptor_hash = hashlib.sha256(raw).hexdigest()
    manifest = {
        "schema_version": 1, "source_game_sha256": GAME_SHA,
        "client_version": "1.5.3", "hot_version": 65,
        "descriptor_sha256": descriptor_hash, "descriptor_size": len(raw),
        "selected_roots": list(expected.values()), "rpc_bindings": report["rpc_bindings"],
        "selected_wire_fields": promoted,
        "source_evidence": str(EVIDENCE.relative_to(ROOT)).replace("\\", "/"),
        "source_evidence_sha256": EVIDENCE_SHA,
        "source_methods": [{key: m[key] for key in
                            ("owner", "method", "method_token", "method_body_sha256")}
                           for m in report["methods"]],
        "projection": "Only season activity identity/time fields are retained; unrelated fields are protobuf unknowns.",
        "season_id_expression": "system_id < 0 ? -1 : ((system_id / 100) % 10) * 10 + ((system_id / 10) % 10)",
        "lumi_tag_expression": "season_id > 0 ? season_id + 1 : 0",
        "active_interval": "begin_time <= current_server_time <= end_time",
        "target_execution": False, "network_requests_sent": False,
        "private_account_archives_read": False, "source_assemblies_persisted": False,
    }
    (DESTINATION / "season.descriptor.pb").write_bytes(raw)
    (DESTINATION / "season-source.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"descriptor_sha256": descriptor_hash, "descriptor_size": len(raw)}))


if __name__ == "__main__":
    main()
