from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CHAIN_IDS = {"ethereum": 1, "monad": 143}


class ScopeContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    program: str
    chain: Literal["ethereum", "monad"]
    contracts: list[ScopeContract] = Field(min_length=1)


def load_scope(path: Path) -> Scope:
    scope = Scope.model_validate_json(path.read_text())
    duplicates = [address for address, count in Counter(c.address.lower() for c in scope.contracts).items() if count > 1]
    if duplicates:
        raise ValueError(f"{path} lists these addresses more than once: {', '.join(duplicates)}")
    return scope
