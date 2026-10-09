"""Read-only parser for the pre-migration source.json format."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from agentbridge.core.capability_runtime import CapabilityRejected

POLICY = "taihua.personal.api-visible.v1"  # Historical config format, migration only.

def reject(code: str) -> None:
    raise CapabilityRejected(code, "旧数据源配置无效，无法迁移。")


@dataclass(frozen=True)
class LegacySourceConfig:
    enabled: bool = False
    source_id: str = "taihua_primary"
    host: str = "10.10.50.101"
    port: int = 35432
    dbname: str = "sisyphus"
    username: str = ""
    sslmode: str = "disable"
    credential_version: int = 1
    policy_version: str = POLICY
    admitted_subjects: tuple[str, ...] = field(default_factory=tuple)
    single_instance: bool = True
    privileges_reviewed: bool = False
    privilege_policy: str = "strict"

    @classmethod
    def load(cls, path: Path) -> "LegacySourceConfig":
        if not path.exists():
            return cls()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            config = cls(**value)
        except (OSError, ValueError, TypeError):
            reject("SOURCE_UNAVAILABLE")
        return config


    def validate(self) -> None:
        if self.privilege_policy not in ("strict", "controlled_readonly_pilot"):
            reject("SOURCE_UNAVAILABLE")
        if any(type(value) is not bool for value in (self.enabled, self.single_instance, self.privileges_reviewed)):
            reject("SOURCE_UNAVAILABLE")
        if (self.source_id != "taihua_primary" or self.policy_version != POLICY
                or self.sslmode not in {"disable", "verify-full"}
                or not self.username or type(self.port) is not int
                or not 1 <= self.port <= 65535 or type(self.credential_version) is not int
                or self.credential_version < 1):
            reject("SOURCE_UNAVAILABLE")
        if self.sslmode == "disable" and (self.host, self.port, self.dbname) != ("10.10.50.101", 35432, "sisyphus"):
            reject("SOURCE_UNAVAILABLE")
        if not isinstance(self.admitted_subjects, (tuple, list)) or any(not isinstance(s, str) or not s for s in self.admitted_subjects):
            reject("SOURCE_UNAVAILABLE")
