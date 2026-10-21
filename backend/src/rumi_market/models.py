from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

Category = Literal["all", "color", "normal", "item"]
SessionStatus = Literal["anonymous", "authenticating", "needs_verification",
                        "role_selection", "connected", "expired"]
# JSON.stringify may encode each allowed character as a six-byte \uXXXX escape.
LOGIN_PLAINTEXT_MAX_BYTES = 6 * (256 + 4096) + len(b'{"account":"","password":"","remember_login":false}')
LOGIN_CIPHERTEXT_MAX_CHARS = 4 * ((LOGIN_PLAINTEXT_MAX_BYTES + 16 + 2) // 3)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(InputModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    account: str = Field(min_length=1, max_length=256, repr=False)
    password: SecretStr = Field(min_length=1, max_length=4096)


class WebLoginRequest(LoginRequest):
    # Accept the old encrypted preference for cached clients, but never act on it.
    remember_login: bool = False


class SmsSendRequest(InputModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    mobile: str = Field(pattern=r"^[0-9]{5,20}$", repr=False)
    country_code: str = Field(pattern=r"^[1-9][0-9]{0,3}$", repr=False)


class SmsLoginRequest(SmsSendRequest):
    otp: SecretStr = Field(min_length=6, max_length=6)

    @model_validator(mode="after")
    def validate_otp(self):
        value = self.otp.get_secret_value()
        if not value.isascii() or not value.isdigit():
            raise ValueError("Invalid verification code")
        return self


class EncryptedLoginRequest(InputModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    challenge_id: str = Field(pattern=r"^[A-Za-z0-9_-]{32}$")
    encrypted_key: SecretStr = Field(min_length=1, max_length=344)
    iv: SecretStr = Field(min_length=1, max_length=16)
    ciphertext: SecretStr = Field(min_length=1, max_length=LOGIN_CIPHERTEXT_MAX_CHARS)


class LoginChallengeView(BaseModel):
    challenge_id: str
    public_key: str
    algorithm: Literal["RSA-OAEP-256+A256GCM"] = "RSA-OAEP-256+A256GCM"
    expires_at: datetime


class RoleRequest(InputModel):
    role_id: str = Field(min_length=1, max_length=256)


class RefreshRequest(InputModel):
    category: Category = "all"
    query: str = Field(default="", max_length=256)
    pet_ids: list[str] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=10, le=10, strict=True)

    @model_validator(mode="after")
    def validate_ranked_query(self):
        if self.limit is not None and self.category != "color":
            raise ValueError("Bounded query requires color")
        if self.limit is None and (self.query.strip() or self.pet_ids):
            raise ValueError("Filtered refresh requires a limit")
        return self


class Role(BaseModel):
    id: str
    name: str
    server_name: str


class SessionView(BaseModel):
    status: SessionStatus = "anonymous"
    authenticated: bool = False
    role: Role | None = None
    roles: list[Role] = Field(default_factory=list)
    message: str | None = None


class PersonalInfoView(InputModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    role_id: str | None = None
    nickname: str | None = None
    server_name: str | None = None
    balance_gold: str | None = None


class Capabilities(BaseModel):
    provider: str = "bilibili_game"
    integration_ready: bool = False
    methods: list[Literal["account", "sms", "qr"]] = Field(default_factory=list)
    reason_code: str | None = "AUTH_PROTOCOL_UNVERIFIED"
    message: str | None = "游戏登录协议尚未接通"
    remember_login: bool = False


class QrView(BaseModel):
    challenge_id: str
    qr_payload: str
    expires_at: datetime


class QrPollView(BaseModel):
    status: Literal["waiting", "scanned", "confirmed", "expired", "failed"]
    session: SessionView | None = None
    message: str | None = None


class PetView(BaseModel):
    pet_id: str
    name: str | None = None
    display_name: str
    image_url: str | None = None
    name_verified: bool = False


class PetResponse(BaseModel):
    items: list[PetView]
    total: int
    named_count: int
    image_count: int


class PetElement(InputModel):
    id: int = Field(strict=True, ge=1)
    name: str = Field(min_length=1)


class PetCatalogItem(PetView):
    elements: list[PetElement] = Field(default_factory=list)


class PetCatalogResponse(PetResponse):
    items: list[PetCatalogItem]


class PetStatRange(InputModel):
    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    min: float = Field(strict=True, allow_inf_nan=False)
    max: float = Field(strict=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_range(self):
        if self.min > self.max:
            raise ValueError("Invalid pet stat range")
        return self


class PetNormalAttack(InputModel):
    element: PetElement | None = None
    power: int | None = Field(default=None, strict=True, ge=0)
    energy_gain: float | None = Field(default=None, strict=True, ge=0, allow_inf_nan=False)
    interval_seconds: float | None = Field(default=None, strict=True, gt=0, allow_inf_nan=False)


class PetSkill(InputModel):
    skill_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    element: PetElement | None = None
    cost_text: str | None = None
    power: int | None = Field(default=None, strict=True, ge=0)
    image_url: str | None = None


class PetTrait(InputModel):
    trait_id: str = Field(min_length=1)
    kind: Literal["battle", "home"]
    name: str = Field(min_length=1)
    description: str | None = None


class PetDetail(PetView):
    available: bool = False
    pokedex_id: str | None = None
    elements: list[PetElement] = Field(default_factory=list)
    stats: list[PetStatRange] = Field(default_factory=list)
    normal_attack: PetNormalAttack | None = None
    traits: list[PetTrait] = Field(default_factory=list)
    fixed_skill: PetSkill | None = None
    replaceable_skills: list[PetSkill] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class Listing(BaseModel):
    listing_key: str
    category: Literal["color", "normal", "item"]
    pet_id: str | None = None
    name: str | None = None
    display_name: str
    image_url: str | None = None
    direct_buy_price: int = Field(ge=0, strict=True)
    expires_at: datetime | None = None
    observed_at: datetime


class NameCoverage(BaseModel):
    named: int = 0
    total: int = 0


class MarketSnapshot(BaseModel):
    refresh_id: str | None = None
    scope: Literal["all", "top10"] = "all"
    status: Literal["idle", "loading", "complete", "partial", "failed"] = "idle"
    complete: bool = False
    items: list[Listing] = Field(default_factory=list)
    loaded_count: int = 0
    matched_count: int = 0
    total_count: int | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    message: str | None = None
    name_coverage: NameCoverage = Field(default_factory=NameCoverage)
