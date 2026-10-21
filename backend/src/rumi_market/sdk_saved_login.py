"""Strict, server-internal saved login contract; never a browser response."""

from .errors import ApiError


def expiry_value(value: object) -> str | None:
    """Preserve an actual SDK timestamp, with no invented lifetime/default."""
    if value is None:
        return None
    if type(value) is int and 0 <= value < 2**63:
        return str(value)
    if (type(value) is str and value.isascii() and value.isdecimal()
            and 1 <= len(value) <= 19 and int(value) < 2**63):
        return value
    raise ValueError("Invalid SDK expiry")


def renewal_threshold(expires: str | None, now_ms: int) -> str | None:
    """CommonTools.parseExpires: absolute milliseconds, Java integer division.

    This is an early renewal threshold, never a credential rejection deadline.
    Unlike the native missing-value fallback, unknown expiry remains unknown.
    """
    if expires is None:
        return None
    deadline = int(expiry_value(expires))
    delta = (deadline - now_ms) * 2
    return str(now_ms + (abs(delta) // 3 if delta >= 0 else -(abs(delta) // 3)))


def valid_login_text(value: object, maximum: int) -> bool:
    return (type(value) is str and 0 < len(value) <= maximum
            and all(33 <= ord(character) <= 126 for character in value))


def copy_login_state(state: object) -> dict:
    """Validate and copy the only fields allowed in encrypted persistence."""
    valid = False
    if type(state) is dict and set(state) == {
            "version", "uid", "access_key", "game_user", "expires", "renewal_expires", "identity"}:
        user, identity = state["game_user"], state["identity"]
        valid = (type(state["version"]) is int and state["version"] == 1
            and valid_login_text(state["uid"], 128) and valid_login_text(state["access_key"], 4096)
            and type(user) is dict and set(user) == {"game_uid", "login_type"}
            and valid_login_text(user["game_uid"], 128) and type(user["login_type"]) is str
            and user["login_type"] == "0"
            and type(identity) is dict and set(identity) == {"udid", "bd_id", "game_device_id"}
            and valid_login_text(identity["udid"], 128) and valid_login_text(identity["bd_id"], 256)
            and valid_login_text(identity["game_device_id"], 128)
            and all(value is None or (type(value) is str and value.isascii()
                    and value.isdecimal() and 1 <= len(value) <= 19 and int(value) < 2**63)
                    for value in (state["expires"], state["renewal_expires"])))
    if not valid:
        raise ApiError("AUTH_REJECTED", "保存的登录信息已失效，请重新登录", 401)
    return {"version": 1, "uid": state["uid"], "access_key": state["access_key"],
        "game_user": dict(state["game_user"]), "expires": state["expires"],
        "renewal_expires": state["renewal_expires"], "identity": dict(state["identity"])}


def make_login_state(credentials, identity, game_device_id: str) -> dict:
    return copy_login_state({"version": 1, "uid": credentials.uid,
        "access_key": credentials.access_key.get_secret_value(),
        "game_user": {"game_uid": credentials.game_user.game_uid,
                      "login_type": credentials.game_user.login_type},
        "expires": credentials.expires, "renewal_expires": credentials.renewal_expires,
        "identity": {"udid": identity.udid, "bd_id": identity.bd_id,
                     "game_device_id": game_device_id}})
