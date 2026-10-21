"""Generate a hash-pinned hot71 public protocol subset, offline and RAM-only."""
import hashlib
import json
import os
from pathlib import Path
import sys
import types

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
DEST = PROJECT / "backend/src/rumi_market/game"
BINDINGS = {
    2919: "Cmd_Notify_Chat_Auth_Session",
    4009: "Cmd_Home_VisitShareQuery_Req", 4010: "Cmd_Home_VisitShareQuery_Ack",
    4011: "Cmd_Home_VisitGetShareAward_Req", 4012: "Cmd_Home_VisitGetShareAward_Ack",
    23903: "Cmd_JoinWorldChannel_Req", 23904: "Cmd_JoinWorldChannel_Ack",
    23909: "Cmd_Notify_ChatMessage_Broadcast", 23910: "Cmd_Notify_ChatMessage_History",
    23920: "Cmd_Auth_Req", 23921: "Cmd_Auth_Ack",
    23922: "Cmd_Reconnect_Req", 23923: "Cmd_Reconnect_Ack",
    23938: "Cmd_Chat_Auth_Session_Req", 23939: "Cmd_Chat_Auth_Session_Ack",
    23940: "Cmd_Notify_HomeVisitShareUpdate",
}
EXTRA_ROOTS = ("SNKProto.WsClientPkg", "SNKProto.HomeVisitShareCardParam")


def integer(instruction):
    op = str(instruction.opcode)
    if op in {"ldc.i4", "ldc.i4.s"}:
        return instruction.operand
    if op.startswith("ldc.i4.") and op[-1].isdigit():
        return int(op[-1])
    return None


def main():
    sys.dont_write_bytecode = True
    outputs = {DEST / "redbag.descriptor.pb", DEST / "redbag-source.json"}
    def guard(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"}:
            raise RuntimeError("Offline public metadata only")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).absolute()
            if any(p.lower() in {"runtime", "account-archives", "profile-archives"} for p in path.parts):
                raise RuntimeError("Private runtime paths forbidden")
            writing = (isinstance(args[1], str) and any(c in args[1] for c in "wax+"))
            writing |= bool(args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if writing and path not in outputs:
                raise RuntimeError("Only selected public resources may be written")
    sys.addaudithook(guard)
    sys.path[:0] = [str(ROOT / p) for p in (
        "artifacts/rumi/home-analysis", "artifacts/rumi/pet-catalog",
        "artifacts/rumi/adventure-analysis")] + [str(PROJECT / "scripts")]
    from lgeb_public_bundle_probe import source_dependencies
    source_dependencies(ROOT)
    from home_hot71_resources import load_hot71_public_ram, GAME_SHA
    from pet_details_fast_source_probe import context
    from adventure_config_probe import _reference
    from managed_descriptor_probe import extract_descriptors_ram
    from protocol_schema_subset import subset_descriptor_set
    from dncil.cil.body import CilMethodBody
    from dncil.cil.body.reader import CilMethodBodyReaderBytes
    game, tables, _, source = load_hot71_public_ram()
    del tables
    ctx = context(game, GAME_SHA)
    def method(self, token):
        row = self.pe.net.mdtables.MethodDef.rows[(token & 0xffffff) - 1]
        rva = row.struct.Rva
        section = self.pe.get_section_by_rva(rva)
        if section is None or not rva:
            raise ValueError("Missing public method body")
        offset = self.pe.get_offset_from_rva(rva)
        end = section.PointerToRawData + section.SizeOfRawData
        return row, CilMethodBody(CilMethodBodyReaderBytes(game[offset:min(end, offset + 1024 * 1024)]))
    ctx.method_token = types.MethodType(method, ctx)
    ctx.reference = types.MethodType(_reference, ctx)
    _, body = ctx.method("ProtoCmdTable", "ResolveParser")
    expected = {rpc: "SNKProto." + name for rpc, name in BINDINGS.items()}
    parsers = {i.offset: ctx.reference(i.operand) for i in body.instructions
               if str(i.opcode) == "call" and ctx.reference(i.operand).get("name") == "get_Parser"
               and ctx.reference(i.operand).get("owner") in expected.values()}
    bindings = []
    for index, ins in enumerate(body.instructions):
        if (str(ins.opcode) != "switch" or index < 3
                or str(body.instructions[index - 1].opcode) != "sub"):
            continue
        base = integer(body.instructions[index - 2])
        if base is None:
            continue
        for slot, target in enumerate(ins.operand):
            if base + slot in expected and target in parsers:
                bindings.append({"rpc_id": base + slot, "message_type": parsers[target]["owner"],
                    "parser_token": parsers[target]["token"], "switch_base": base,
                    "switch_index": slot, "switch_il_offset": ins.offset, "parser_il_offset": target})
    if {b["rpc_id"]: b["message_type"] for b in bindings} != expected or len(bindings) != len(expected):
        raise ValueError("The selected hot71 RPC binding changed")
    reflections = {n for n in ctx.types if n.split(".")[0] in {
        "SNKProto", "SNKCacheEx", "SNKGMProto", "Ex", "SNKProtoMs"} and n.endswith("Reflection")}
    extracted = extract_descriptors_ram(game, reflection_types=reflections)
    roots = sorted(set(expected.values()) | set(EXTRA_ROOTS))
    subset = subset_descriptor_set(extracted.files, ["." + name for name in roots])
    raw = subset.SerializeToString(deterministic=True)
    metadata = {"schema_version": 1, "client_version": "1.5.3", "hot_version": 71,
        "source_game_sha256": source["game_sha256"], "catalog_sha256": source["catalog_sha256"],
        "source_report_sha256": source["source_report_sha256"],
        "cache_manifest_sha256": source["cache_manifest_sha256"],
        "descriptor_sha256": hashlib.sha256(raw).hexdigest(), "descriptor_size": len(raw),
        "selected_roots": roots, "rpc_bindings": sorted(bindings, key=lambda b: b["rpc_id"]),
        "source_file_descriptors": [{k: row[k] for k in ("name", "descriptor_sha256")}
                                    for row in extracted.report["files"]],
        "resolver_body_sha256": hashlib.sha256(body.raw_bytes).hexdigest(),
        "producer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "network_requests_sent": False, "private_archives_read": False, "target_execution": False,
        "plaintext_assemblies_or_tables_saved": False}
    (DEST / "redbag.descriptor.pb").write_bytes(raw)
    (DEST / "redbag-source.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"descriptor_size": len(raw), "descriptor_sha256": metadata["descriptor_sha256"],
                      "known_commands": sorted(expected)}))


if __name__ == "__main__":
    main()
