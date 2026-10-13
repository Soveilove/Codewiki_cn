from pydantic import BaseModel
from typing import Any
from codewiki.src.be.dependency_analyzer.models.core import Node, CallRelationship, Repository


class AnalysisResult(BaseModel):
    """Result of analyzing a repository"""

    repository: Repository
    functions: list[Node]
    relationships: list[CallRelationship]
    file_tree: dict[str, Any]
    summary: dict[str, Any]
    visualization: dict[str, Any] = {}
    readme_content: str | None = None


class NodeSelection(BaseModel):
    """Selected nodes for partial export"""

    selected_nodes: list[str] = []
    include_relationships: bool = True
    custom_names: dict[str, str] = {}
