import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from harness.config import Config, load_config


class ConfigTests(unittest.TestCase):
    def test_invalid_limits(self):
        for values in ({"max_turns": 0}, {"max_turns": True}, {"max_retries": 11},
                       {"request_timeout_seconds": float("nan")},
                       {"total_timeout_seconds": float("inf")},
                       {"retry_base_seconds": -1}, {"max_output_chars": 10},
                       {"completion_token_parameter": "arbitrary"}, {"runs_dir": ""}):
            with self.assertRaises(ValueError):
                replace(Config(model="test-model", base_url="https://test.invalid/v1"), **values).validate()

    def test_endpoint_requirements(self):
        for url in ("", "ftp://host/v1", "http://remote/v1", "https://key@host/v1", "https://host/v1?key=secret"):
            with self.assertRaises(ValueError):
                Config(provider="openai_compatible", model="model", base_url=url).validate()
        Config(provider="openai_compatible", model="model", base_url="http://127.0.0.1:1234/v1").validate()
        with self.assertRaises(ValueError):
            Config(provider="openai_compatible", base_url="https://host/v1").validate()

    def test_config_env_precedence_and_explicit_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"model": "file-model", "base_url": "https://test.invalid/v1"}), encoding="utf-8")
            env = Path(tmp) / ".env"
            env.write_text("HARNESS_MODEL=dotenv-model\nHARNESS_API_KEY=dummy-secret\n", encoding="utf-8")
            os.environ["HARNESS_MODEL"] = "environment-model"
            config = load_config(path, env)
            self.assertEqual(config.model, "environment-model")
            self.assertNotIn("dummy-secret", json.dumps(config.snapshot()))

    def test_config_rejects_secret_fields_and_missing_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"api_key":"dummy-secret"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)
            with self.assertRaises(ValueError):
                load_config(env_file=Path(tmp) / "missing")

    def test_cli_provider_override_precedes_validation(self):
        with patch.dict(os.environ, {"HARNESS_PROVIDER": "unsupported",
                                     "HARNESS_MODEL": "test-model", "HARNESS_BASE_URL": "https://test.invalid/v1"}, clear=True):
            self.assertEqual(load_config(overrides={"provider": "openai_compatible"}).provider, "openai_compatible")


if __name__ == "__main__":
    unittest.main()
