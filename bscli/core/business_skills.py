"""Versioned business instructions and user assignments; never grants business access."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from importlib.resources import files
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
from uuid import uuid4

from bscli.core.user_grants import UserGrantConflict

PROTOCOL = "agentbridge.skills.v1"
META_KEY = "agentbridge/skill"


class SkillRejected(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def skill_bundle(binding):
    """Load the pinned entrypoint and profile requirements, never a caller path."""
    snapshot = binding["snapshot"]
    manifest = snapshot["manifest"]
    # Historical v1 snapshots predate the declaration; their references were mandatory.
    required = manifest.get("required_resources", {}).get(binding["profile"], manifest.get("resources", []))
    names = [manifest["entrypoint"], *required]
    if any(name not in snapshot["resources"] for name in names):
        raise SkillRejected("SKILL_VERSION_UNAVAILABLE", "业务助手必读资料不完整，请联系管理员")
    return {"loaded_resources": names,
            "content": "\n\n".join(f"## {name}\n\n{snapshot['resources'][name]}" for name in names)}


def validate_skill_bundle(manifest, resources):
    """Validate both packaged and authored immutable instruction bundles."""
    if not isinstance(manifest, dict) or not isinstance(resources, dict):
        raise ValueError("invalid Skill bundle")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", str(manifest.get("id", ""))):
        raise ValueError("invalid Skill id")
    if manifest.get("schemaVersion") != PROTOCOL or not re.fullmatch(r"\d+\.\d+\.\d+", str(manifest.get("version", ""))):
        raise ValueError("unsupported Skill manifest")
    if any(not isinstance(manifest.get(key), str) or not manifest[key].strip() or len(manifest[key]) > 500 for key in ("name", "description", "entrypoint")):
        raise ValueError("invalid Skill metadata")
    if manifest.get("entrypoint") != "SKILL.md" or manifest.get("status") not in ("enabled", "trial", "disabled"):
        raise ValueError("invalid Skill entrypoint or status")
    declared = manifest.get("resources", [])
    if not isinstance(declared, list) or any(not isinstance(n, str) for n in declared):
        raise ValueError("invalid Skill resource paths")
    names = [manifest["entrypoint"], *declared]
    if len(names) != len(set(names)) or len(names) > 16 or set(names) != set(resources):
        raise ValueError("invalid Skill resources")
    for name, content in resources.items():
        if not isinstance(name, str): raise ValueError("invalid Skill resource path")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or path.suffix != ".md" or not isinstance(content, str):
            raise ValueError("invalid Skill resource")
    if sum(len(v) for v in resources.values()) > 48000:
        raise ValueError("Skill resource budget exceeded")
    if not isinstance(manifest.get("profiles"), dict) or not manifest["profiles"] or manifest.get("executionMode") not in ("read_exploration", "durable_plan", "controlled_action"):
        raise ValueError("invalid Skill execution contract")
    selection = manifest.get("selection")
    if not isinstance(selection, dict) or set(selection) != {"use_when", "not_for", "output"} or any(
        not isinstance(v, str) or not v.strip() or len(v) > 500 for v in selection.values()
    ):
        raise ValueError("invalid Skill selection contract")
    required = manifest.get("required_resources")
    if not isinstance(required, dict) or set(required) != set(manifest["profiles"]):
        raise ValueError("invalid Skill required resources by profile")
    for names in required.values():
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names) or len(names) != len(set(names)) or any(
            n == "SKILL.md" or n not in resources for n in names
        ):
            raise ValueError("invalid Skill required resource")
    from bscli.core.user_grants import PERMISSIONS
    for profile, requirements in manifest["profiles"].items():
        if not isinstance(profile, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", profile) or not isinstance(requirements, dict) or set(requirements) - {"all", "any", "database"}:
            raise ValueError('模式仅允许权限依赖 all、any、database；无需业务权限时使用 {"use":{}}，不要填写 description 或 tools')
        for mode in ("all", "any"):
            deps = requirements.get(mode, [])
            if not isinstance(deps, list) or any(not isinstance(p, str) or p not in PERMISSIONS for p in deps):
                raise ValueError("unknown Skill business dependency")
        if "database" in requirements:
            from bscli.database.independent import CAPABILITIES
            deps = requirements["database"]
            if not isinstance(deps, dict) or set(deps) - {"all", "any"} or any(not isinstance(v, list) or any(not isinstance(c, str) or c not in CAPABILITIES for c in v) for v in deps.values()):
                raise ValueError("unknown Skill database dependency")
    payload = {"manifest": manifest, "resources": resources}
    return {**payload, "content_hash": sha256(_json(payload).encode()).hexdigest()}


class SkillRegistry:
    def __init__(self, root=None):
        root = root or files("bscli.business_skills")
        self.items = {}
        for directory in sorted(root.iterdir(), key=lambda p: p.name):
            if not directory.is_dir() or not directory.joinpath("manifest.json").is_file():
                continue
            if isinstance(directory, Path) and (directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve())):
                raise ValueError("Skill directory escapes package")
            manifest = json.loads(directory.joinpath("manifest.json").read_text(encoding="utf-8"))
            sid = manifest.get("id", "")
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", sid) or sid != directory.name or sid in self.items:
                raise ValueError("invalid or duplicate Skill id")
            resources = {}
            for name in [manifest["entrypoint"], *manifest.get("resources", [])]:
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or path.suffix != ".md":
                    raise ValueError("invalid Skill resource path")
                resource = directory.joinpath(*path.parts)
                if isinstance(resource, Path) and not resource.resolve().is_relative_to(directory.resolve()):
                    raise ValueError("Skill resource escapes package")
                resources[name] = resource.read_text(encoding="utf-8")
            self.items[sid] = validate_skill_bundle(manifest, resources)

    def get(self, sid):
        if sid not in self.items:
            raise SkillRejected("SKILL_NOT_FOUND", "业务助手不存在")
        return self.items[sid]


class SkillStore:
    def __init__(self, db_path, registry=None):
        self.db_path = Path(db_path)
        self.registry = registry or SkillRegistry()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS skill_publications (
                    skill_id TEXT PRIMARY KEY, version TEXT NOT NULL, owner_subject TEXT NOT NULL,
                    request_id TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_load_events (
                    event_id TEXT PRIMARY KEY, user_subject TEXT NOT NULL, dedupe_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(user_subject, dedupe_key));
                CREATE INDEX IF NOT EXISTS skill_load_events_user_time ON skill_load_events(user_subject, created_at);
                CREATE TABLE IF NOT EXISTS skill_config (
                    owner TEXT PRIMARY KEY, value_json TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_events (
                    event_id TEXT PRIMARY KEY, owner TEXT NOT NULL, actor TEXT NOT NULL,
                    reason TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_versions (
                    skill_id TEXT NOT NULL, version TEXT NOT NULL, content_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY(skill_id, version));
                CREATE TABLE IF NOT EXISTS skill_bindings (
                    binding_id TEXT PRIMARY KEY, user_subject TEXT NOT NULL, skill_id TEXT NOT NULL,
                    version TEXT NOT NULL, profile TEXT NOT NULL, settings_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS skill_task_bindings (
                    task_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL, user_subject TEXT NOT NULL);
            """)
            for sid, item in self.registry.items.items():
                version = item["manifest"]["version"]
                old = db.execute("SELECT content_hash FROM skill_versions WHERE skill_id=? AND version=?", (sid, version)).fetchone()
                if old and old[0] != item["content_hash"]:
                    raise ValueError("Skill version has different content; increment version")
                db.execute("INSERT OR IGNORE INTO skill_versions VALUES (?,?,?,?)", (sid, version, item["content_hash"], _json(item)))

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def current(self, sid, connection=None):
        if connection is None:
            with closing(self.connect()) as db:
                return self.current(sid, db)
        row = connection.execute("SELECT v.payload_json FROM skill_publications p JOIN skill_versions v ON v.skill_id=p.skill_id AND v.version=p.version WHERE p.skill_id=?", (sid,)).fetchone()
        return json.loads(row[0]) if row else self.registry.get(sid)

    def all_items(self):
        with closing(self.connect()) as db:
            authored = {r["skill_id"]: json.loads(r["payload_json"]) for r in db.execute(
                "SELECT p.skill_id,v.payload_json FROM skill_publications p JOIN skill_versions v ON v.skill_id=p.skill_id AND v.version=p.version")}
        return {**self.registry.items, **authored}

    def record_load(self, subject, sid, profile, resource, result):
        if result.get("status") == "succeeded" and resource != "SKILL.md":
            return  # Loading a reference does not trigger another assistant.
        item = self.all_items().get(sid) if sid in self.registry.items or sid in self.config("user:" + subject)["value"] else None
        payload = {"skill_id": sid[:80], "name": item["manifest"]["name"] if item else "未知业务助手",
                   "profile": profile[:80], "status": result.get("status"),
                   "version": result.get("version"), "error_code": (result.get("error") or {}).get("code"),
                   "loaded_resources": result.get("loaded_resources", []),
                   "message": (result.get("error") or {}).get("message")}
        event_id = str(uuid4())
        dedupe = result.get("binding_id") or event_id
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO skill_load_events VALUES (?,?,?,?,?)",
                       (event_id, subject, dedupe, _json(payload), datetime.now(timezone.utc).isoformat()))

    def load_history(self, subject, limit=50):
        with closing(self.connect()) as db:
            return [{"event_id": row["event_id"], "created_at": row["created_at"], **json.loads(row["payload_json"])}
                    for row in db.execute("SELECT * FROM skill_load_events WHERE user_subject=? ORDER BY created_at DESC LIMIT ?",
                                          (subject, min(max(limit, 1), 100)))]

    def task_presentation(self, subject, task_id):
        # Historical attribution remains visible after revocation; it grants no access.
        with closing(self.connect()) as db:
            row = db.execute("SELECT b.* FROM skill_bindings b JOIN skill_task_bindings t USING(binding_id) "
                             "WHERE t.task_id=? AND t.user_subject=? AND b.user_subject=?", (task_id, subject, subject)).fetchone()
            if not row:
                return None
            version = db.execute("SELECT payload_json FROM skill_versions WHERE skill_id=? AND version=?",
                                 (row["skill_id"], row["version"])).fetchone()
            manifest = json.loads(version[0])["manifest"] if version else {}
            return {"skill_id": row["skill_id"], "name": manifest.get("name", row["skill_id"]),
                    "version": row["version"], "profile": row["profile"], "revoked": bool(row["revoked"])}

    def config(self, owner, connection=None):
        if connection is None:
            with closing(self.connect()) as db:
                return self.config(owner, db)
        row = connection.execute("SELECT * FROM skill_config WHERE owner=?", (owner,)).fetchone()
        return {"value": json.loads(row["value_json"]) if row else {}, "revision": row["revision"] if row else 0}

    def save(self, owner, value, *, expected_revision, actor, reason, audit_callback=None):
        if not isinstance(owner, str) or not owner or not isinstance(value, dict) or len(value) > 100:
            raise ValueError("invalid Skill configuration")
        if type(expected_revision) is not int or expected_revision < 0 or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("invalid revision or reason")
        normalized = {}
        for sid, settings in value.items():
            manifest = self.current(sid)["manifest"]
            if owner == "global":
                if settings not in ("enabled", "trial", "disabled"):
                    raise ValueError("invalid Skill status")
                normalized[sid] = settings
            else:
                if not owner.startswith("user:") or not isinstance(settings, dict) or set(settings) - {"profiles", "source_id", "detail"}:
                    raise ValueError("invalid Skill settings")
                profiles = settings.get("profiles", list(manifest["profiles"]))
                if not isinstance(profiles, list) or not profiles or any(p not in manifest["profiles"] for p in profiles) or len(set(profiles)) != len(profiles):
                    raise ValueError("invalid Skill profiles")
                source = settings.get("source_id", "")
                detail = settings.get("detail", "standard")
                if not isinstance(source, str) or (source and not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", source)) or detail not in {"brief", "standard", "detailed"}:
                    raise ValueError("invalid Skill defaults")
                normalized[sid] = {"profiles": profiles, "source_id": source, "detail": detail}
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if owner.startswith("user:"):
                for sid, settings in normalized.items():
                    self._approved_scope(owner[5:], sid, settings["profiles"], db)
            before = self.config(owner, db)
            if before["revision"] != expected_revision:
                raise UserGrantConflict("Skill 配置已被修改，请重新打开")
            after = {"value": normalized, "revision": expected_revision + 1}
            db.execute("INSERT INTO skill_config VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET value_json=excluded.value_json, revision=excluded.revision", (owner, _json(normalized), after["revision"]))
            bindings = db.execute("SELECT * FROM skill_bindings WHERE revoked=0" + (" AND user_subject=?" if owner.startswith("user:") else ""), (owner[5:],) if owner.startswith("user:") else ()).fetchall()
            for binding in bindings:
                if (owner == "global" and normalized.get(binding["skill_id"]) == "disabled") or (owner.startswith("user:") and binding["profile"] not in normalized.get(binding["skill_id"], {}).get("profiles", [])):
                    db.execute("UPDATE skill_bindings SET revoked=1 WHERE binding_id=?", (binding["binding_id"],))
            db.execute("INSERT INTO skill_events VALUES (?,?,?,?,?,?,?)", (str(uuid4()), owner, actor, reason.strip(), _json(before), _json(after), datetime.now(timezone.utc).isoformat()))
            if audit_callback:
                audit_callback(db, before, after)
            return after

    def _approved_scope(self, subject, sid, profiles, db):
        if not db.execute("SELECT 1 FROM skill_publications WHERE skill_id=?", (sid,)).fetchone():
            return
        row = db.execute("SELECT r.audience_json,r.profiles_json FROM skill_publications p JOIN skill_review_requests r ON r.request_id=p.request_id WHERE p.skill_id=?", (sid,)).fetchone()
        if row and (subject not in json.loads(row['audience_json']) or not set(profiles) <= set(json.loads(row['profiles_json']))):
            raise SkillRejected("SKILL_APPROVAL_SCOPE", "超出已审批使用范围，请作者重新提交审批")

    def _allowed(self, subject, sid, profile, connection):
        global_config = self.config("global", connection)
        state = global_config["value"].get(sid, self.current(sid, connection)["manifest"].get("status", "trial"))
        settings = self.config("user:" + subject, connection)["value"].get(sid)
        if state == "disabled" or not settings or profile not in settings["profiles"]:
            raise SkillRejected("SKILL_UNAVAILABLE", "业务助手未分配、已停用或所选功能已关闭")
        self._approved_scope(subject, sid, [profile], connection)
        return settings

    def available_item(self, subject, sid, profile):
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            # Check assignment before reading private publication metadata.
            if sid not in self.config("user:" + subject, db)["value"]:
                raise SkillRejected("SKILL_UNAVAILABLE", "业务助手未分配或不可访问")
            self._allowed(subject, sid, profile, db)
            return self.current(sid, db)

    def bind(self, subject, sid, profile, source_id=None, expected_version=None):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            item = self.current(sid, db)
            if expected_version and expected_version != item["manifest"]["version"]:
                raise SkillRejected("SKILL_VERSION_CHANGED", "业务助手版本已更新，请刷新目录")
            settings = self._allowed(subject, sid, profile, db)
            settings = {**settings, "source_id": source_id or settings["source_id"]}
            binding = str(uuid4())
            db.execute("INSERT INTO skill_bindings (binding_id,user_subject,skill_id,version,profile,settings_json,created_at) VALUES (?,?,?,?,?,?,?)", (binding, subject, sid, item["manifest"]["version"], profile, _json(settings), datetime.now(timezone.utc).isoformat()))
            return binding

    def binding(self, subject, binding_id, connection=None):
        if connection is None:
            with closing(self.connect()) as db:
                return self.binding(subject, binding_id, db)
        row = connection.execute("SELECT * FROM skill_bindings WHERE binding_id=? AND user_subject=?", (binding_id, subject)).fetchone()
        if row is None:
            raise SkillRejected("SKILL_BINDING_INVALID", "业务助手任务绑定无效")
        if row["revoked"]:
            raise SkillRejected("SKILL_BINDING_REVOKED", "本次业务助手已被撤销，请重新开始任务")
        result = dict(row)
        self._allowed(subject, row["skill_id"], row["profile"], connection)
        version = connection.execute("SELECT payload_json FROM skill_versions WHERE skill_id=? AND version=?", (row["skill_id"], row["version"])).fetchone()
        if version is None:
            raise SkillRejected("SKILL_VERSION_UNAVAILABLE", "业务助手原版本不可用")
        result["snapshot"] = json.loads(version[0])
        result["settings"] = json.loads(row["settings_json"])
        return result

    def attach(self, subject, task_id, binding_id):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.binding(subject, binding_id, db)
            old = db.execute("SELECT * FROM skill_task_bindings WHERE task_id=?", (task_id,)).fetchone()
            if old and (old["binding_id"] != binding_id or old["user_subject"] != subject):
                raise SkillRejected("SKILL_TASK_CONFLICT", "任务已有另一业务助手绑定")
            db.execute("INSERT OR IGNORE INTO skill_task_bindings VALUES (?,?,?)", (task_id, binding_id, subject))

    def for_task(self, subject, task_id, connection=None):
        if not task_id:
            return None
        if connection is None:
            with closing(self.connect()) as db:
                return self.for_task(subject, task_id, db)
        row = connection.execute("SELECT binding_id FROM skill_task_bindings WHERE task_id=? AND user_subject=?", (task_id, subject)).fetchone()
        return self.binding(subject, row[0], connection) if row else None


def dependency_state(service, subject, manifest, profile, source_id=None):
    """Evaluate actual grants; a Skill manifest is not an authorization."""
    requirements = manifest["profiles"][profile]
    granted = set((service.user_grants.get(subject) or {}).get("permissions", []))
    from bscli.core.user_grants import PERMISSIONS
    missing = [PERMISSIONS[p]["label"] for p in requirements.get("all", []) if p not in granted]
    alternatives = requirements.get("any", [])
    if alternatives and not granted.intersection(alternatives):
        missing.append("适用业务权限至少一项，例如：" + "、".join(PERMISSIONS[p]["label"] for p in alternatives[:2]))
    if requirements.get("database"):
        from bscli.database.independent import IndependentDatabase
        catalog = IndependentDatabase(service.home).catalog(subject)
        eligible = []
        for source in catalog["sources"]:
            capabilities = {c["name"] for c in source["capabilities"]}
            required = requirements["database"]
            if all(c in capabilities for c in required.get("all", [])) and (not required.get("any") or capabilities.intersection(required["any"])):
                eligible.append(source["source_id"])
        if source_id and source_id not in eligible:
            missing.append("所选数据源缺少查询或分析权限")
        elif not eligible:
            missing.append("没有具备所需能力的数据源")
        return {"available": not missing and bool(source_id), "missing": missing,
                "needs_source": not source_id and bool(eligible), "sources": eligible}
    return {"available": not missing, "missing": missing, "needs_source": False, "sources": []}


def skill_catalog(service, subject, *, include_all=False):
    store = service.skills
    global_config = store.config("global")
    user = store.config("user:" + subject)
    items = []
    for sid, item in store.all_items().items():
        settings = user["value"].get(sid)
        if not include_all and not settings:
            continue
        manifest = item["manifest"]
        profiles = settings["profiles"] if settings else list(manifest["profiles"])
        items.append({"id": sid, "name": manifest["name"], "description": manifest["description"],
                      "selection": manifest["selection"],
                      "version": manifest["version"], "content_hash": item["content_hash"],
                      "status": global_config["value"].get(sid, manifest.get("status", "trial")),
                      "assigned": bool(settings), "settings": settings,
                      "requires_source": any("database" in p for p in manifest["profiles"].values()),
                      "profiles": {p: dependency_state(service, subject, manifest, p, (settings or {}).get("source_id")) for p in profiles},
                      "profile_choices": list(manifest["profiles"])})
    return {"schemaVersion": PROTOCOL, "items": items, "revision": user["revision"],
            "global_revision": global_config["revision"], "has_more": False}


def validate_binding(service, subject, binding, *, capability=None):
    manifest = binding["snapshot"]["manifest"]
    state = dependency_state(service, subject, manifest, binding["profile"], binding["settings"]["source_id"])
    if not state["available"]:
        raise SkillRejected("SKILL_DEPENDENCY_UNAVAILABLE", "业务助手依赖不可用：" + "；".join(state["missing"]))
    if capability and binding["profile"] == "preview":
        if service.registry.get(capability).effect != "read":
            raise SkillRejected("SKILL_PREVIEW_ONLY", "本次业务助手仅允许预览")
    if capability and binding["profile"] == "single" and "batch" in capability:
        raise SkillRejected("SKILL_SINGLE_ONLY", "本次业务助手仅允许单条办理")
