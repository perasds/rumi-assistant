class ApiError(Exception):
    """Only application-owned messages may cross the HTTP boundary."""

    def __init__(self, code: str, message: str, status_code: int = 503,
                 retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message,
                          "retryable": self.retryable}}


def auth_required() -> ApiError:
    return ApiError("AUTH_REQUIRED", "请先登录游戏", 401)


def provider_error(error: ApiError) -> ApiError:
    """Do not trust upstream text or unknown error codes, even in ApiError."""
    safe_errors = {
        "AUTH_PROTOCOL_UNVERIFIED": ("游戏登录协议尚未接通", 503, False),
        "MARKET_PROTOCOL_UNVERIFIED": ("游戏商品协议尚未接通", 503, False),
        "AUTH_REJECTED": ("游戏认证信息未通过验证", 401, False),
        "AUTH_SMS_REJECTED": ("验证码未通过验证，请检查后重试", 401, False),
        "GAME_SESSION_EXPIRED": ("游戏会话已过期，请重新登录", 401, False),
        "AUTH_UPSTREAM_FAILED": ("游戏认证服务暂不可用", 503, True),
        "AUTH_CAPTCHA_CANCELLED": ("已取消人工验证码", 401, False),
        "AUTH_CAPTCHA_FAILED": ("人工验证码未完成", 401, False),
        "AUTH_CAPTCHA_UNAVAILABLE": ("本机验证窗口暂不可用", 503, False),
        "AUTH_CAPTCHA_PAGE_UNAVAILABLE": ("官方验证页暂不可用", 503, False),
        "AUTH_CAPTCHA_TIMEOUT": ("人工验证已超时", 503, False),
        "AUTH_CAPTCHA_INVALID": ("验证码返回数据未通过校验", 503, False),
    }
    if error.code in safe_errors:
        message, status, retryable = safe_errors[error.code]
        return ApiError(error.code, message, status, retryable)
    return ApiError("UPSTREAM_UNAVAILABLE", "游戏服务暂不可用", 503, True)
