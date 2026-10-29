"""Data and model version manifest helpers."""

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class VersionedArtifact:
    name: str
    kind: str
    path: str
    checksum: str

    def to_dict(self):
        return {
            "name": self.name,
            "kind": self.kind,
            "path": self.path,
            "checksum": self.checksum,
        }


@dataclass(frozen=True)
class ArtifactManifest:
    run_id: str
    git_commit: str
    artifacts: List[VersionedArtifact]

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "git_commit": self.git_commit,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    def to_dvc_stage(self, command: str):
        return {
            "cmd": command,
            "deps": [artifact.path for artifact in self.artifacts if artifact.kind == "data"],
            "outs": [artifact.path for artifact in self.artifacts if artifact.kind == "model"],
            "meta": {
                "run_id": self.run_id,
                "git_commit": self.git_commit,
                "checksums": {
                    artifact.name: artifact.checksum
                    for artifact in self.artifacts
                },
            },
        }
