"""Validate centrally scoped original Workspace media; never fetch a supplied URL."""
import base64
import hashlib
from pathlib import PurePosixPath

from .media import DOCUMENT_FORMATS, FORMATS, MAX_FILE
from .pi_workspace_result_cards import timestamp


def resource(value):
    if not isinstance(value,dict):
        return None
    name,mime,size,digest,content=(value.get(k) for k in ("fileName","mimeType","byteSize","sha256","contentBase64"))
    if (not isinstance(name,str) or not 0<len(name)<=256 or name in {".",".."}
            or PurePosixPath(name).name!=name or "\\" in name or any(ord(c)<32 or ord(c)==127 for c in name)
            or mime not in FORMATS|DOCUMENT_FORMATS or type(size) is not int or not 0<size<=MAX_FILE
            or not isinstance(content,str) or len(content)>4*((MAX_FILE+2)//3)
            or timestamp(value.get("expiresAt")) is None):
        return None
    try:
        name.encode("utf-8")
        data=base64.b64decode(content,validate=True)
    except (UnicodeError,ValueError):
        return None
    signature=(FORMATS|DOCUMENT_FORMATS)[mime][1]
    if (len(data)!=size or hashlib.sha256(data).hexdigest()!=digest or not data.startswith(signature)
            or mime=="image/webp" and data[8:12]!=b"WEBP"):
        return None
    return {"fileName":name,"mimeType":mime,"byteSize":size,"sha256":digest,
            "contentBase64":content,"expiresAt":value["expiresAt"]}


def message(value):
    if any(value.get(k) is not None for k in ("publicText","resultView","interaction")):
        return None
    item=resource(value.get("resource"))
    if not item:
        return None
    return {"filename":item["fileName"],"content_type":item["mimeType"],
            "data":base64.b64decode(item["contentBase64"],validate=True)},timestamp(item["expiresAt"])
