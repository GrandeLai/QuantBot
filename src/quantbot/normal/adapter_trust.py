"""Trusted deployment-adapter attestations for formal normal admission."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import marshal
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..evidence import hash_payload


NORMAL_ADAPTER_ATTESTATION_VERSION = 1
NORMAL_ADAPTER_ATTESTATION_PATH_ENV = (
    "STOCKBOT_NORMAL_ADAPTER_ATTESTATION_PATH"
)
TRUSTED_NORMAL_ADAPTER_KEYS_ENV = "STOCKBOT_TRUSTED_NORMAL_ADAPTER_KEYS_JSON"


def _sha256_digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class NormalAdapterArtifactIdentity:
    """Core-computed identity of the executable adapter and schema mapping."""

    adapter_artifact_hash: str
    schema_mapping_hash: str

    def __post_init__(self) -> None:
        _sha256_digest(self.adapter_artifact_hash, "adapter_artifact_hash")
        _sha256_digest(self.schema_mapping_hash, "schema_mapping_hash")


def normal_adapter_artifact_identity(loader) -> NormalAdapterArtifactIdentity:
    """Hash executable bytes/source and mapping content, ignoring self-reported hashes."""

    module = inspect.getmodule(loader)
    module_path = getattr(module, "__file__", "")
    if module_path and Path(module_path).is_file():
        artifact_kind = "module_artifact"
        executable_hash = hashlib.sha256(Path(module_path).read_bytes()).hexdigest()
    else:
        try:
            source = inspect.getsource(loader).encode("utf-8")
        except (OSError, TypeError) as error:
            raise ValueError(
                "normal adapter identity requires inspectable source or a module artifact"
            ) from error
        artifact_kind = "python_source"
        executable_hash = hashlib.sha256(source).hexdigest()

    implementation_target = (
        loader
        if inspect.isfunction(loader)
        else loader.__func__
        if inspect.ismethod(loader)
        else getattr(type(loader), "__call__", None)
    )
    if implementation_target is None:
        raise ValueError("normal adapter must be an inspectable callable")
    try:
        implementation = inspect.getsource(implementation_target).encode("utf-8")
        implementation_kind = "python_source"
    except (OSError, TypeError):
        code = getattr(implementation_target, "__code__", None)
        if code is None:
            raise ValueError("normal adapter callable implementation is not inspectable")
        implementation = marshal.dumps(code)
        implementation_kind = "python_code_object"
    implementation_hash = hashlib.sha256(implementation).hexdigest()

    schema_mapping = getattr(loader, "stockbot_schema_mapping", None)
    if (
        schema_mapping is None
        and getattr(loader, "stockbot_schema_mapping_hash", "")
    ):
        raise ValueError(
            "stockbot_schema_mapping_hash is self-attested; expose "
            "stockbot_schema_mapping content instead"
        )
    schema_mapping_hash = hash_payload(
        schema_mapping if schema_mapping is not None else {"kind": "code_defined"}
    )
    adapter_artifact_hash = hash_payload(
        {
            "module": getattr(loader, "__module__", type(loader).__module__),
            "qualname": getattr(loader, "__qualname__", type(loader).__qualname__),
            "artifact_kind": artifact_kind,
            "executable_hash": executable_hash,
            "implementation_kind": implementation_kind,
            "implementation_hash": implementation_hash,
            "schema_mapping_hash": schema_mapping_hash,
        }
    )
    return NormalAdapterArtifactIdentity(
        adapter_artifact_hash=adapter_artifact_hash,
        schema_mapping_hash=schema_mapping_hash,
    )


def require_formal_normal_adapter(loader) -> None:
    """Reject runtime-bound callables that an artifact signature cannot attest."""

    if not inspect.isfunction(loader):
        raise ValueError("formal normal adapter must be a plain Python function")
    if loader.__closure__ or loader.__defaults__ or loader.__kwdefaults__:
        raise ValueError(
            "formal normal adapter cannot capture closure or default-argument state"
        )


@dataclass(frozen=True, slots=True)
class TrustedNormalAdapterAttestation:
    """Verified issuer assertion binding one adapter to one PIT bundle identity."""

    issuer: str
    adapter_artifact_hash: str
    schema_mapping_hash: str
    bundle_provider: str
    bundle_schema_version: str
    bundle_id: str
    bundle_dataset_hash: str
    content_hash: str
    signature: str

    def __post_init__(self) -> None:
        for name in ("issuer", "bundle_provider", "bundle_schema_version", "bundle_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        for name in (
            "adapter_artifact_hash",
            "schema_mapping_hash",
            "bundle_dataset_hash",
            "content_hash",
            "signature",
        ):
            _sha256_digest(getattr(self, name), name)

    @property
    def unsigned_payload(self) -> dict[str, object]:
        return {
            "artifact_version": NORMAL_ADAPTER_ATTESTATION_VERSION,
            "issuer": self.issuer,
            "adapter_artifact_hash": self.adapter_artifact_hash,
            "schema_mapping_hash": self.schema_mapping_hash,
            "bundle_provider": self.bundle_provider,
            "bundle_schema_version": self.bundle_schema_version,
            "bundle_id": self.bundle_id,
            "bundle_dataset_hash": self.bundle_dataset_hash,
        }


_ATTESTATION_FIELDS = frozenset(
    {
        "artifact_version",
        "issuer",
        "adapter_artifact_hash",
        "schema_mapping_hash",
        "bundle_provider",
        "bundle_schema_version",
        "bundle_id",
        "bundle_dataset_hash",
        "content_hash",
        "signature",
    }
)


def verify_normal_adapter_attestation(
    path: Path,
    *,
    adapter_identity: NormalAdapterArtifactIdentity,
    bundle_manifest,
    bundle_dataset_hash: str,
    trusted_keys: Mapping[str, bytes] | None = None,
    environ: Mapping[str, str] | None = None,
) -> TrustedNormalAdapterAttestation:
    """Load and verify an external attestation before sealed exposure is claimed."""

    path = Path(path)
    if not path.is_absolute():
        raise ValueError("normal adapter attestation path must be absolute")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("normal adapter attestation is not readable JSON") from error
    if not isinstance(payload, dict) or set(payload) != _ATTESTATION_FIELDS:
        raise ValueError("normal adapter attestation fields are invalid")
    if payload.get("artifact_version") != NORMAL_ADAPTER_ATTESTATION_VERSION:
        raise ValueError("normal adapter attestation version is unsupported")
    attestation = TrustedNormalAdapterAttestation(
        issuer=payload["issuer"],
        adapter_artifact_hash=payload["adapter_artifact_hash"],
        schema_mapping_hash=payload["schema_mapping_hash"],
        bundle_provider=payload["bundle_provider"],
        bundle_schema_version=payload["bundle_schema_version"],
        bundle_id=payload["bundle_id"],
        bundle_dataset_hash=payload["bundle_dataset_hash"],
        content_hash=payload["content_hash"],
        signature=payload["signature"],
    )
    expected_identity = {
        "adapter_artifact_hash": adapter_identity.adapter_artifact_hash,
        "schema_mapping_hash": adapter_identity.schema_mapping_hash,
        "bundle_provider": bundle_manifest.provider,
        "bundle_schema_version": bundle_manifest.schema_version,
        "bundle_id": bundle_manifest.bundle_id,
        "bundle_dataset_hash": _sha256_digest(
            bundle_dataset_hash, "bundle_dataset_hash"
        ),
    }
    mismatches = [
        name
        for name, expected in expected_identity.items()
        if getattr(attestation, name) != expected
    ]
    if mismatches:
        raise PermissionError(
            "normal adapter attestation identity mismatch: "
            + ",".join(mismatches)
        )
    if attestation.content_hash != hash_payload(attestation.unsigned_payload):
        raise PermissionError("normal adapter attestation content_hash mismatch")
    keys = (
        dict(trusted_keys)
        if trusted_keys is not None
        else _trusted_keys_from_environment(environ)
    )
    key = keys.get(attestation.issuer)
    if key is None:
        raise PermissionError("trusted normal adapter issuer key is missing")
    expected_signature = hmac.new(
        key,
        attestation.content_hash.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, attestation.signature):
        raise PermissionError("normal adapter attestation signature is invalid")
    return attestation


def _trusted_keys_from_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, bytes]:
    source = os.environ if environ is None else environ
    raw = source.get(TRUSTED_NORMAL_ADAPTER_KEYS_ENV, "").strip()
    if not raw:
        raise PermissionError(
            f"{TRUSTED_NORMAL_ADAPTER_KEYS_ENV} must configure trusted issuer keys"
        )
    try:
        payload = json.loads(raw)
        keys = {
            str(issuer): bytes.fromhex(value)
            for issuer, value in payload.items()
        }
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise PermissionError(
            f"{TRUSTED_NORMAL_ADAPTER_KEYS_ENV} must be a JSON object of hex keys"
        ) from error
    if not keys or any(len(key) < 16 for key in keys.values()):
        raise PermissionError(
            f"{TRUSTED_NORMAL_ADAPTER_KEYS_ENV} keys must contain at least 16 bytes"
        )
    return keys


__all__ = (
    "NORMAL_ADAPTER_ATTESTATION_PATH_ENV",
    "TRUSTED_NORMAL_ADAPTER_KEYS_ENV",
    "NormalAdapterArtifactIdentity",
    "TrustedNormalAdapterAttestation",
    "normal_adapter_artifact_identity",
    "require_formal_normal_adapter",
    "verify_normal_adapter_attestation",
)
