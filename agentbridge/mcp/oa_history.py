"""Closed selection input shared by the model-visible OA history tools."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class OaHistorySelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["latest"]
    count: Annotated[int, Field(strict=True, ge=1, le=50)]
    workflow_state: Literal["all", "in_progress", "ended"]
