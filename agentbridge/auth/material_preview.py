"""Authenticated, same-origin material bytes; no source URL or reusable ticket."""
from __future__ import annotations

import json
import re

from agentbridge.auth.card import AuthCardResponse
from agentbridge.core.material_previews import BusinessMaterialPreviewService


def material_preview_target(path):
    match = re.fullmatch(r"/flow/input/([A-Za-z0-9_-]{32,128})(?:/present/([A-Za-z0-9_-]{32,128}))?"
                         r"/draft/materials/([a-f0-9]{64})", path)
    return (match[1], match[2], match[3]) if match else None


class BusinessMaterialPreviewApplication:
    def __init__(self, *, draft_application):
        self.draft_application = draft_application

    def get_file(self, source_submission_id, *, presentation_id=None, material_id, token, csrf_cookie):
        try:
            value = BusinessMaterialPreviewService(self.draft_application.backend).fetch(
                source_submission_id, presentation_id=presentation_id, material_id=material_id,
                csrf_token=token, csrf_cookie=csrf_cookie)
            body = (json.dumps(value["archive"], ensure_ascii=False).encode()
                    if value.get("contentType") == "application/json" else value["body"])
            return AuthCardResponse(200, {
                "Content-Type": value["contentType"], "Content-Length": str(len(body)),
                "Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "same-origin",
                "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'",
            }, body)
        except Exception as error:
            return self.draft_application._error(error)
