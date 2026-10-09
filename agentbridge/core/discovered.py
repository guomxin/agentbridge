from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from pathlib import Path
from typing import Any


DISCOVERED_API_SCHEMA = "agentbridge.discovered_api.v1"
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


@dataclass(frozen=True)
class DiscoveredApi:
    system: str
    name: str
    description: str
    access: str
    risk: str
    request: dict[str, Any]
    inspection: dict[str, Any]
    path: Path
    raw: dict[str, Any]
    parameters: dict[str, Any] = field(default_factory=dict)

    @property
    def command_name(self) -> str:
        return f"discovered:{self.name}"

    @property
    def tool_name(self) -> str:
        return f"{self.system}__discovered__{_tool_slug(self.name)}"

    @property
    def method(self) -> str:
        return str(self.request.get("method") or "GET").upper()

    @property
    def requires_confirmation(self) -> bool:
        return self.access != "read" or self.risk != "low" or self.method != "GET"


class DiscoveredApiStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def list_apis(self, system: str) -> list[DiscoveredApi]:
        api_dir = self._api_dir(system)
        if not api_dir.exists():
            return []
        apis = []
        for path in sorted(api_dir.glob("*.json")):
            apis.append(self._load_path(system, path))
        return sorted(apis, key=lambda api: api.name)

    def load_api(self, system: str, name: str) -> DiscoveredApi:
        self._validate_name(name)
        path = self._api_dir(system) / f"{name}.json"
        if not path.exists():
            raise KeyError(f"discovered API not found: {system}.{name}")
        return self._load_path(system, path)

    def _api_dir(self, system: str) -> Path:
        self._validate_name(system)
        return self.root / "discovered" / system / "apis"

    def _load_path(self, system: str, path: Path) -> DiscoveredApi:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != DISCOVERED_API_SCHEMA:
            raise ValueError(f"unsupported discovered API schema: {path}")
        name = str(data.get("name") or path.stem)
        self._validate_name(name)
        request = data.get("request") or {}
        if not isinstance(request, dict) or not request.get("url"):
            raise ValueError(f"discovered API request.url is required: {path}")
        inspection = data.get("inspection") or {}
        parameters = data.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise ValueError(f"discovered API parameters must be an object: {path}")
        _validate_parameter_schema(parameters)
        return DiscoveredApi(
            system=system,
            name=name,
            description=str(data.get("description") or ""),
            access=str(data.get("access") or "read"),
            risk=str(data.get("risk") or "low"),
            request=request,
            inspection=inspection if isinstance(inspection, dict) else {},
            path=path,
            raw=data,
            parameters=parameters,
        )

    def _validate_name(self, value: str) -> None:
        if not value or not _SAFE_NAME_RE.match(value):
            raise ValueError(f"unsafe discovered API name: {value}")


def _tool_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()


def render_discovered_request(api: DiscoveredApi, arguments: dict[str, Any]) -> dict[str, Any]:
    parameter_args = {
        name: value
        for name, value in arguments.items()
        if name not in {"name", "confirm"}
    }
    _validate_parameter_args(api.parameters, parameter_args)
    return _render_template(api.request, parameter_args)


def _validate_parameter_schema(parameters: dict[str, Any]) -> None:
    for name, spec in parameters.items():
        if not _SAFE_NAME_RE.match(str(name)) or str(name) in {"name", "confirm"}:
            raise ValueError(f"unsafe discovered API parameter name: {name}")
        if spec is not None and not isinstance(spec, dict):
            raise ValueError(f"discovered API parameter spec must be an object: {name}")


def _validate_parameter_args(parameters: dict[str, Any], arguments: dict[str, Any]) -> None:
    for name in arguments:
        if name not in parameters:
            raise ValueError(f"unexpected discovered API argument: {name}")
    for name, spec in parameters.items():
        spec = spec or {}
        if spec.get("required") and name not in arguments:
            raise ValueError(f"missing required discovered API argument: {name}")
        if name in arguments:
            _validate_parameter_type(name, arguments[name], str(spec.get("type") or "string"))


def _validate_parameter_type(name: str, value: Any, expected_type: str) -> None:
    if expected_type == "string" and not isinstance(value, str):
        raise ValueError(f"discovered API argument must be string: {name}")
    if expected_type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"discovered API argument must be integer: {name}")
    if expected_type == "number" and (not isinstance(value, int | float) or isinstance(value, bool)):
        raise ValueError(f"discovered API argument must be number: {name}")
    if expected_type == "boolean" and not isinstance(value, bool):
        raise ValueError(f"discovered API argument must be boolean: {name}")


def _render_template(value: Any, arguments: dict[str, Any]) -> Any:
    if isinstance(value, str):
        full_match = re.fullmatch(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}", value)
        if full_match:
            return arguments[full_match.group(1)]
        return re.sub(
            r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}",
            lambda match: str(arguments[match.group(1)]),
            value,
        )
    if isinstance(value, list):
        return [_render_template(item, arguments) for item in value]
    if isinstance(value, dict):
        return {key: _render_template(item, arguments) for key, item in value.items()}
    return value
