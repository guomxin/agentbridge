from __future__ import annotations

from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from .task_driver import ReferenceTaskDriver


class ArtifactDriver:
    def __init__(self, task_driver: "ReferenceTaskDriver") -> None:
        self.task_driver = task_driver
        self._by_task: dict[str, list[dict[str, Any]]] = {}
        self._private_urls: dict[tuple[str, str], str] = {}

    def update_from_snapshot(
        self,
        local_task_id: str,
        artifacts: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        for key in [key for key in self._private_urls if key[0] == local_task_id]:
            self._private_urls.pop(key, None)
        public: list[dict[str, Any]] = []
        for raw in artifacts:
            artifact_id = str(raw.get("artifactId") or "").strip()
            if not artifact_id:
                continue
            status = str(raw.get("status") or "UNKNOWN").upper()
            download_url = raw.get("downloadUrl")
            if status == "READY" and isinstance(download_url, str) and download_url.startswith(
                ("http://", "https://")
            ):
                self._private_urls[(local_task_id, artifact_id)] = download_url
            public.append(
                {
                    "artifactId": artifact_id,
                    "fileName": str(raw.get("fileName") or "download"),
                    "mediaType": str(
                        raw.get("mediaType") or "application/octet-stream"
                    ),
                    "size": max(int(raw.get("size") or 0), 0),
                    "status": status,
                    "expiresAt": raw.get("expiresAt"),
                    "regenerable": raw.get("regenerable") is True,
                    "downloadPath": (
                        f"/api/tasks/{local_task_id}/artifacts/{artifact_id}/download"
                    ),
                    "reissuePath": (
                        f"/api/tasks/{local_task_id}/artifacts/{artifact_id}/reissue"
                    ),
                }
            )
        self._by_task[local_task_id] = public
        return list(public)

    def list_for_task(self, local_task_id: str) -> list[dict[str, Any]]:
        return list(self._by_task.get(local_task_id, []))

    async def download_url(
        self,
        local_task_id: str,
        artifact_id: str,
    ) -> str:
        await self.task_driver.refresh_task_snapshot(local_task_id)
        value = self._private_urls.get((local_task_id, artifact_id))
        if not value:
            raise KeyError("artifact download is unavailable or expired")
        return value

    async def reissue(
        self,
        local_task_id: str,
        artifact_id: str,
    ) -> dict[str, Any]:
        task = self.task_driver.state.get_task(local_task_id)
        lease = await self.task_driver.renew_coordinator_lease(local_task_id)
        result = await self.task_driver.call_for_task(
            local_task_id,
            "agentbridge_host_artifact_reissue",
            {
                "agent_host": self.task_driver.implementation.name,
                "task_id": task["task_id"],
                "artifact_id": artifact_id,
            },
            lease=lease,
            recovery_class="unsafe",
        )
        await self.task_driver.refresh_task_snapshot(local_task_id)
        return result.payload()
