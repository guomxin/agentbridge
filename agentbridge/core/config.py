from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class SystemProfile:
    id: str
    name: str
    base_url: str
    allowed_origins: list[str]
    auth_mode: str = "central_session"

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("system id is required")
        if not self.base_url:
            raise ValueError("base_url is required")
        if not self.allowed_origins:
            raise ValueError("allowed_origins is required")
        if self.base_origin not in self.allowed_origins:
            raise ValueError("base_url origin must be included in allowed_origins")
        if self.auth_mode != "central_session":
            raise ValueError("auth_mode must be central_session")

    @property
    def base_origin(self) -> str:
        parsed = urlparse(self.base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("base_url must include scheme and host")
        return f"{parsed.scheme}://{parsed.netloc}"


class ConfigStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.systems_dir = self.root / "systems"

    def save_system(self, profile: SystemProfile) -> None:
        self.systems_dir.mkdir(parents=True, exist_ok=True)
        path = self.systems_dir / f"{profile.id}.json"
        path.write_text(
            json.dumps(asdict(profile), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_system(self, system_id: str) -> SystemProfile:
        path = self.systems_dir / f"{system_id}.json"
        if not path.exists():
            raise KeyError(f"system not found: {system_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return _profile_from_data(data)

    def list_systems(self) -> list[SystemProfile]:
        if not self.systems_dir.exists():
            return []
        profiles = []
        for path in sorted(self.systems_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            profiles.append(_profile_from_data(data))
        return profiles


def _profile_from_data(data: dict) -> SystemProfile:
    return SystemProfile(**data)
