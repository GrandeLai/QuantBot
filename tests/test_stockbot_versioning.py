import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.versioning import ArtifactManifest, VersionedArtifact


class StockBotVersioningTest(unittest.TestCase):
    def test_artifact_manifest_exports_dvc_friendly_data_and_model_versions(self):
        manifest = ArtifactManifest(
            run_id="2026-06-26",
            git_commit="abc123",
            artifacts=[
                VersionedArtifact(name="ohlcv_2010_2024", kind="data", path="data/ohlcv", checksum="sha256:data"),
                VersionedArtifact(name="hmm_regime_v1", kind="model", path="models/hmm.pkl", checksum="sha256:model"),
            ],
        )

        payload = manifest.to_dict()
        dvc_stage = manifest.to_dvc_stage(command="quantbot schedule")

        self.assertEqual(payload["git_commit"], "abc123")
        self.assertEqual(payload["artifacts"][0]["kind"], "data")
        self.assertEqual(dvc_stage["cmd"], "quantbot schedule")
        self.assertEqual(dvc_stage["deps"], ["data/ohlcv"])
        self.assertEqual(dvc_stage["outs"], ["models/hmm.pkl"])


if __name__ == "__main__":
    unittest.main()
