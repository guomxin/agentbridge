"""Ordered construction of central persistence, separate from business dispatch."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bscli.admin.stores import GovernancePolicyStore
from bscli.core.auth_challenges import AuthChallengeStore
from bscli.core.business_skills import SkillStore
from bscli.core.document_downloads import DocumentDownloadStore
from bscli.core.field_submissions import FieldSubmissionStore
from bscli.core.host_contract import HostContractStore
from bscli.core.interactions import InteractionStore
from bscli.core.operations import OperationStore
from bscli.core.runtime_governance import RuntimeGovernanceStore
from bscli.core.session_secrets import SessionStateStore
from bscli.core.sessions import SessionRegistry
from bscli.core.task_plans import TaskPlanStore
from bscli.core.tasks import TaskHubStore
from bscli.core.timeline_attachments import TimelineAttachmentStore
from bscli.core.transforms import TransformRegistry, build_transform_registry
from bscli.core.user_grants import UserGrants
from bscli.core.write_authorizations import WriteAuthorizationStore
from bscli.workspace.stores import WorkspaceStore


@dataclass(frozen=True)
class CentralAccessStores:
    """The stores needed before skill authoring and capability registration."""

    user_grants: UserGrants
    skills: SkillStore

    @classmethod
    def create(cls, db_path: Path) -> CentralAccessStores:
        return cls(user_grants=UserGrants(db_path), skills=SkillStore(db_path))


@dataclass(frozen=True)
class CentralRuntimeStores:
    operations: OperationStore
    sessions: SessionRegistry
    session_states: SessionStateStore
    challenges: AuthChallengeStore
    field_submissions: FieldSubmissionStore
    document_downloads: DocumentDownloadStore
    timeline_attachments: TimelineAttachmentStore
    write_authorizations: WriteAuthorizationStore
    interactions: InteractionStore
    tasks: TaskHubStore
    task_plans: TaskPlanStore
    transforms: TransformRegistry
    host_contract: HostContractStore
    workspace: WorkspaceStore
    governance_policies: GovernancePolicyStore
    runtime_governance: RuntimeGovernanceStore

    @classmethod
    def create(
        cls, home: Path, *, session_state_store: SessionStateStore | None,
        release_id: str,
    ) -> CentralRuntimeStores:
        db_path = home / "agentbridge.db"
        operations = OperationStore(db_path)
        sessions = SessionRegistry(db_path, home / "profiles", maintain_on_startup=False)
        sessions.run_startup_maintenance()
        session_states = session_state_store or SessionStateStore(home / "session-secrets")
        challenges = AuthChallengeStore(db_path)
        field_submissions = FieldSubmissionStore(db_path)
        document_downloads = DocumentDownloadStore(db_path)
        timeline_attachments = TimelineAttachmentStore(db_path)
        write_authorizations = WriteAuthorizationStore(db_path)
        interactions = InteractionStore(db_path)
        # Repairs read operations and interactions, so initialize those ledgers first.
        tasks = TaskHubStore(db_path, maintain_on_startup=False)
        tasks.run_startup_maintenance()
        task_plans = TaskPlanStore(db_path)
        transforms = build_transform_registry()
        host_contract = HostContractStore(db_path)
        workspace = WorkspaceStore(db_path)
        governance_policies = GovernancePolicyStore(db_path)
        runtime_governance = RuntimeGovernanceStore(db_path, release_id=release_id)
        return cls(
            operations=operations, sessions=sessions, session_states=session_states,
            challenges=challenges, field_submissions=field_submissions,
            document_downloads=document_downloads, timeline_attachments=timeline_attachments,
            write_authorizations=write_authorizations, interactions=interactions,
            tasks=tasks, task_plans=task_plans, transforms=transforms,
            host_contract=host_contract, workspace=workspace,
            governance_policies=governance_policies, runtime_governance=runtime_governance,
        )
