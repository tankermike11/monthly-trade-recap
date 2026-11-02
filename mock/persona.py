"""Validation for persona YAML files: the shared fields every persona must have.
Behaviour-specific rules are read by each engine; extra keys are allowed here."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PersonaAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    starting_value: float = Field(gt=0)
    options_level: int = Field(ge=0, le=4)
    futures: bool
    crypto: bool


class PersonaConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = Field(pattern=r"^p\d\d$")
    first_name: str
    time_zone: str = Field(pattern=r"^America/(New_York|Chicago|Denver|Los_Angeles)$")
    style: str
    account: PersonaAccount
    summary: str = Field(min_length=40)
