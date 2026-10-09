from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Asset(Model):
    name: str
    url: str | None = None
    address: str
    updateSlot: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{1,64}$")


class Scope(Model):
    program: str
    source: Literal["immunefi", "manual"] = "immunefi"
    chain: Literal["ethereum", "monad"] = "ethereum"
    assets: list[Asset]


class Fingerprint(Model):
    implementation: str | None
    code_hash: str | None


class State(Model):
    fingerprints: dict[str, Fingerprint] = {}


class ClaudeResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    is_error: bool = False
    result: str | None = None
    total_cost_usd: float | None = None
