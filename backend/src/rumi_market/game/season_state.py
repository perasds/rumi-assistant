"""Connection-local season activity state; callers supply the server clock.

Hot65 Player_Activity_Season stores complete activity rows by system_open_id.
8001 initializes and 8002 upserts rows; omitted delta rows are preserved. Current
activity uses inclusive begin/end bounds. Overlap fails closed for purchases,
although the source client's display chooses the first matching dictionary row.
No wall-clock fallback or persisted account state is used here.
"""
from .envelope import ResponseEnvelope
from .season_codec import SeasonProtoCodec

MAX_SEASON_ACTIVITIES = 256


class SeasonState:
    def __init__(self):
        try:
            self._codec = SeasonProtoCodec()
        except ValueError:
            self._codec = None
        self.clear()

    def clear(self) -> None:
        """Reset before login/reconnect and on disconnect; never reuse a baseline."""
        self._activities = {}
        self._baseline = False
        self._login_complete = False
        self._reason = "baseline_pending"

    def mark_login_complete(self) -> None:
        """Called only when the current normal-login handshake has completed."""
        self._login_complete = True

    def _invalidate(self, reason: str) -> None:
        self._activities.clear()
        self._baseline = False
        self._reason = reason

    def observe(self, response: ResponseEnvelope) -> None:
        """Observe complete, deduplicated notifications from the existing client."""
        if not isinstance(response, ResponseEnvelope) or response.command not in (8001, 8002):
            return
        if self._codec is None:
            self._invalidate("schema_unavailable")
            return
        if response.code or response.remaining:
            self._invalidate("notification_invalid")
            return
        if response.command == 8002 and not self._baseline:
            return
        try:
            message = self._codec.decode(response.command, response.data)
            if not message.HasField("activity_info"):
                raise ValueError
            entries = message.activity_info.season_list
            if len(entries) > MAX_SEASON_ACTIVITIES:
                raise ValueError
            updates = {}
            for entry in entries:
                if not entry.HasField("act_comm"):
                    raise ValueError
                comm = entry.act_comm
                names = ("system_open_id", "system_id", "begin_time", "end_time")
                if not all(comm.HasField(name) for name in names):
                    raise ValueError
                row = {name: getattr(comm, name) for name in names}
                if (row["system_open_id"] <= 0 or row["system_id"] < 0
                        or row["begin_time"] < 0 or row["end_time"] < row["begin_time"]):
                    raise ValueError
                updates[row["system_open_id"]] = row
            if len(self._activities.keys() | updates.keys()) > MAX_SEASON_ACTIVITIES:
                raise ValueError
        except Exception:
            self._invalidate("notification_invalid")
            return
        self._activities.update(updates)
        if response.command == 8001:
            self._baseline = True
            self._reason = None

    def snapshot(self, now: int | None = None) -> dict:
        """Resolve a season using the current server-clock seconds, on every call."""
        result = {"valid": False, "current_season_id": None, "current_lumi_tag": None,
                  "system_open_id": None, "begin_time": None, "end_time": None,
                  "unavailable_reason": None}
        if self._codec is None:
            reason = "schema_unavailable"
        elif not self._login_complete:
            reason = "login_pending"
        elif not self._baseline:
            reason = self._reason or "baseline_pending"
        elif type(now) is not int or not 0 <= now < 1 << 63:
            reason = "clock_unavailable"
        else:
            active = [row for row in self._activities.values()
                      if row["begin_time"] <= now <= row["end_time"]]
            if not active:
                reason = "no_active_season"
            elif len(active) != 1:
                reason = "ambiguous_season"
            else:
                row = active[0]
                system = row["system_id"]
                season = ((system // 100) % 10) * 10 + ((system // 10) % 10)
                if season <= 0:
                    reason = "season_id_unavailable"
                else:
                    result.update(valid=True, current_season_id=season, current_lumi_tag=season+1,
                                  system_open_id=row["system_open_id"], begin_time=row["begin_time"],
                                  end_time=row["end_time"])
                    return result
        result["unavailable_reason"] = reason
        return result
