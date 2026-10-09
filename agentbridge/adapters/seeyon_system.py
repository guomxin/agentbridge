from __future__ import annotations

from agentbridge.core.config import SystemProfile


SEEYON_OA_URL = "http://10.10.50.110/seeyon/main.do?method=main"
SEEYON_OA_ORIGIN = "http://10.10.50.110"


def build_seeyon_profile() -> SystemProfile:
    return SystemProfile(
        id="oa",
        name="Seeyon OA",
        base_url=SEEYON_OA_URL,
        allowed_origins=[SEEYON_OA_ORIGIN],
        auth_mode="central_session",
    )
