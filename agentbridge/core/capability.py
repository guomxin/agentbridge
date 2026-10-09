from __future__ import annotations

from dataclasses import asdict, dataclass
import re


_CAPABILITY_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_EFFECTS = {"read", "reversible_write", "controlled_write"}


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    version: str
    description: str
    input_schema: dict
    output_schema: dict
    effect: str
    adapter: str
    workflow: str

    def __post_init__(self) -> None:
        if not _CAPABILITY_NAME_RE.fullmatch(self.name):
            raise ValueError(f"invalid capability name: {self.name}")
        if not _SEMVER_RE.fullmatch(self.version):
            raise ValueError(f"invalid capability version: {self.version}")
        if self.effect not in _EFFECTS:
            raise ValueError(f"invalid capability effect: {self.effect}")
        if not isinstance(self.input_schema, dict) or not isinstance(self.output_schema, dict):
            raise TypeError("capability schemas must be JSON objects")
        if not self.adapter or not self.workflow:
            raise ValueError("capability adapter and workflow are required")

    @property
    def system(self) -> str:
        return self.name.split(".", 1)[0]

    def to_dict(self) -> dict:
        return {**asdict(self), "system": self.system}


class CapabilityRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, CapabilitySpec] = {}

    def register(self, spec: CapabilitySpec) -> None:
        existing = self._specs.get(spec.name)
        if existing is not None and existing != spec:
            raise ValueError(f"capability already registered: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> CapabilitySpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(f"unknown capability: {name}") from exc

    def list(self, *, system: str | None = None) -> list[CapabilitySpec]:
        specs = self._specs.values()
        if system:
            specs = (spec for spec in specs if spec.system == system)
        return sorted(specs, key=lambda spec: (spec.name, spec.version))

    def describe(self, name: str) -> dict:
        return self.get(name).to_dict()
