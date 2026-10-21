"""Promote exact hot65 public order wire metadata, offline and RAM-only."""

import hashlib
import json
from pathlib import Path
import sys
import types


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
DESTINATION = PROJECT / "backend/src/rumi_market/game"
BINDINGS = {2016: "Cmd_Notify_Combine_Info", 3001: "Cmd_Notify_HomeAll",
            3301: "Cmd_Notify_HomeOrderArchiChange", 3302: "Cmd_Home_Order_Complete_Req",
            3303: "Cmd_Home_Order_Complete_Ack", 3304: "Cmd_Home_Order_Refresh_Req",
            3305: "Cmd_Home_Order_Refresh_Ack", 3025: "Cmd_Home_ArchitecturePickWorkReward_Req",
            3026: "Cmd_Home_ArchitecturePickWorkReward_Ack",
            3027: "Cmd_Home_ArchitecturePickAllWorkReward_Req",
            3028: "Cmd_Home_ArchitecturePickAllWorkReward_Ack", 3029: "Cmd_Home_TryUpdateWork_Req",
            3030: "Cmd_Home_TryUpdateWork_Ack", 3040: "Cmd_Notify_AdventureBuffChange"}


def capacity_config(ctx, tables):
    from home_config_probe import derive_schemas
    from luban_pet_decoder import Reader
    schemas, _, derive = derive_schemas(ctx)
    expected = {"AdventureBuff": "f0ddc5608ecfcafd88158a63dca18df7d8ce8eeb15713ffa24c26698ad67a769",
                "AdventureBuffEffect": "08a9303b0c3c482f3b9e4aa6e81d62e08fed190bede07110e5e6e48bc220b61a"}
    rows = {}
    for name, digest in expected.items():
        if hashlib.sha256(tables[name]).hexdigest() != digest:
            raise ValueError("Fixed public capacity configuration changed")
        derive(name)
        reader = Reader(tables[name])
        def read(kind):
            if isinstance(kind, str):
                return getattr(reader, "read_" + kind)()
            category, *args = kind
            if category == "record":
                return record(args[0])
            if category == "optional":
                return read(args[0]) if reader.read_bool() else None
            count = reader.read_size()
            if not 0 <= count <= 100000:
                raise ValueError("Public configuration count limit")
            if category == "list":
                return [read(args[0]) for _ in range(count)]
            if category == "map":
                return {read(args[0]): read(args[1]) for _ in range(count)}
            raise ValueError("Unsupported public configuration schema")
        def record(record_name):
            return {field: read(kind) for field, kind in schemas[record_name]}
        count = reader.read_size()
        if not 0 <= count <= 100000:
            raise ValueError("Public table count limit")
        rows[name] = [record(name) for _ in range(count)]
        if reader.remaining:
            raise ValueError("Unread public configuration suffix")
    capacity_buffs = [row for row in rows["AdventureBuff"] if row["MapBuffType"] == 8]
    building_buffs = [row for row in rows["AdventureBuff"] if row["MapBuffType"] == 9]
    effects = {row["Id"]: row for row in rows["AdventureBuffEffect"]}
    if (len(capacity_buffs) != 1 or capacity_buffs[0]["Id"] != 601
            or capacity_buffs[0]["EffectList"] != [601, 602]
            or any(len(row["EffectList"]) > 1 for row in building_buffs)
            or [effects[602][name] for name in ("Parameter1", "Parameter2", "Parameter3")] != ["300", "200", "100"]):
        raise ValueError("Public capacity configuration semantics changed")
    return {"table_sha256": expected, "strict_eof": True, "buff_id": 601, "buff_map_type": 8,
            "secondary_effect_id": 602, "bonus_by_stage": {"1": 300, "2": 200, "other": 100},
            "building_buff_capacity_bonus": 0, "building_buff_ids": [row["Id"] for row in building_buffs],
            "stage_source": "server_buff_stage", "local_stage_updates": False}


def integer(instruction):
    op = str(instruction.opcode)
    if op in {"ldc.i4", "ldc.i4.s"}:
        return instruction.operand
    if op == "ldc.i4.m1":
        return -1
    if op.startswith("ldc.i4.") and op[-1].isdigit():
        return int(op[-1])
    return None


def main():
    sys.dont_write_bytecode = True
    def deny(event, args):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise RuntimeError("Offline public metadata generation only")
    sys.addaudithook(deny)
    sys.path.insert(0, str(ROOT / "artifacts/rumi/adventure-analysis"))
    from adventure_config_probe import sources, _reference
    from google.protobuf import descriptor_pb2
    ctx, tables, source = sources()
    ctx.reference = types.MethodType(_reference, ctx)
    from managed_descriptor_probe import extract_descriptors_ram
    from protocol_schema_subset import subset_descriptor_set
    extracted = extract_descriptors_ram(ctx._data, reflection_types={
        name for name in ctx.types if name.split(".")[0] in {"SNKProto", "SNKCacheEx", "SNKGMProto", "Ex", "SNKProtoMs"}
        and name.endswith("Reflection")})
    expected = {command: "SNKProto." + name for command, name in BINDINGS.items()}
    _, body = ctx.method_token(0x6000130)
    parsers = {}
    for ins in body.instructions:
        if str(ins.opcode) == "call":
            ref = ctx.reference(ins.operand)
            if ref.get("name") == "get_Parser" and ref.get("owner") in expected.values():
                parsers[ins.offset] = ref
    bindings = []
    for index, ins in enumerate(body.instructions):
        if str(ins.opcode) != "switch" or index < 3 or str(body.instructions[index - 1].opcode) != "sub":
            continue
        base = integer(body.instructions[index - 2])
        if base is None:
            continue
        for slot, target in enumerate(ins.operand):
            command = base + slot
            if command in expected and target in parsers:
                bindings.append({"rpc_id": command, "message_type": parsers[target]["owner"],
                                 "parser_token": parsers[target]["token"], "switch_base": base,
                                 "switch_index": slot, "switch_il_offset": ins.offset,
                                 "parser_il_offset": target, "parser_invoked": False})
    if {row["rpc_id"]: row["message_type"] for row in bindings} != expected or len(bindings) != len(expected):
        raise ValueError("Public parser bindings changed")
    schemas = {file.package + "." + message.name: message
               for file in extracted.files.values() for message in file.message_type}
    # The existing profile resource is reused only after all inventory message
    # definitions (including the opaque change payload's type) match hot65.
    profile = descriptor_pb2.FileDescriptorSet.FromString((DESTINATION / "profile.descriptor.pb").read_bytes())
    profile_schemas = {file.package + "." + message.name: message
                       for file in profile.file for message in file.message_type}
    inventory = {}
    for name in ("Cmd_Notify_Combine_Info", "CmdCombineUnit", "Cmd_Notify_ItemAll",
                 "Cmd_Notify_ItemChange", "CmdRepeatChange", "CmdItemReal"):
        full_name = "SNKProto." + name
        raw = schemas[full_name].SerializeToString(deterministic=True)
        if raw != profile_schemas[full_name].SerializeToString(deterministic=True):
            raise ValueError("Inventory wire schema differs from the existing profile resource")
        inventory[full_name] = hashlib.sha256(raw).hexdigest()
    capacity_profile = {}
    for name in ("Cmd_Notify_AdventureAll", "CmdAdventureBuff"):
        full_name = "SNKProto." + name
        raw = schemas[full_name].SerializeToString(deterministic=True)
        if raw != profile_schemas[full_name].SerializeToString(deterministic=True):
            raise ValueError("Capacity baseline wire schema differs from the profile resource")
        capacity_profile[full_name] = hashlib.sha256(raw).hexdigest()
    consumers = []
    for token, getter in ((0x6000692, "get_OrderArchi"), (0x60006ab, "get_OrderArchi")):
        row, method = ctx.method_token(token)
        refs = [(ins, ctx.reference(ins.operand)) for ins in method.instructions if hasattr(ins.operand, "table")]
        if not any(ref.get("name") == getter for _, ref in refs) or not any(
                str(ins.opcode) == "stfld" and ref.get("name") == "_orderInfo" for ins, ref in refs):
            raise ValueError("Order replacement consumer changed")
        consumers.append({"owner": ctx.owners[(6, token & 0xffffff)],
                          "method": str(ctx.pe.net.strings.get(row.struct.Name_StringIndex)),
                          "token": hex(token), "body_sha256": hashlib.sha256(method.raw_bytes).hexdigest(),
                          "semantics": "replace_order_archi"})
    roots = [expected[command] for command in sorted(expected)]
    descriptor = subset_descriptor_set(extracted.files, ["." + name for name in roots])
    raw = descriptor.SerializeToString(deterministic=True)
    metadata = {"schema_version": 1, "status": "verified_public_wire_schema", "client_version": "1.5.3",
                "hot_version": 65, "source_game_sha256": source["game_sha256"],
                "catalog_sha256": source["catalog_sha256"],
                "encrypted_bundle_sha256": {name: source[name]["source_bundle_sha256"] for name in ("game", "vib")},
                "descriptor_sha256": hashlib.sha256(raw).hexdigest(), "descriptor_size": len(raw),
                "selected_roots": roots, "rpc_bindings": sorted(bindings, key=lambda row: row["rpc_id"]),
                "inventory_profile_message_sha256": inventory, "order_consumers": consumers,
                "capacity_profile_message_sha256": capacity_profile, "capacity_config": capacity_config(ctx, tables),
                "network_requests_sent": False, "private_archives_read": False, "target_execution": False,
                "plaintext_assemblies_or_tables_saved": False}
    (DESTINATION / "order.descriptor.pb").write_bytes(raw)
    (DESTINATION / "order-source.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"descriptor_size": len(raw), "descriptor_sha256": metadata["descriptor_sha256"],
                      "known_commands": sorted(expected), "inventory_types_verified": len(inventory)}))


if __name__ == "__main__":
    main()
