import tempfile
import unittest
import importlib.util
import pathlib


def _load_tls_module():
    path = pathlib.Path(__file__).resolve().parents[1] / "app" / "core" / "tls.py"
    spec = importlib.util.spec_from_file_location("tls_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tls = _load_tls_module()


class TLSPolicyTests(unittest.TestCase):
    def test_disabled_tls_returns_empty_files(self):
        files = tls.resolve_tls_server_files(
            use_self_signed=False,
            certfile="/tmp/missing-cert.pem",
            keyfile="/tmp/missing-key.pem",
            component_name="API",
        )
        self.assertIsNone(files.certfile)
        self.assertIsNone(files.keyfile)

    def test_enabled_tls_requires_files(self):
        with self.assertRaisesRegex(RuntimeError, "Webhook TLS files are missing"):
            tls.resolve_tls_server_files(
                use_self_signed=True,
                certfile="/tmp/missing-cert.pem",
                keyfile="/tmp/missing-key.pem",
                component_name="Webhook",
            )

    def test_enabled_tls_accepts_existing_files(self):
        with tempfile.NamedTemporaryFile() as cert, tempfile.NamedTemporaryFile() as key:
            files = tls.resolve_tls_server_files(
                use_self_signed=True,
                certfile=cert.name,
                keyfile=key.name,
                component_name="API",
            )
        self.assertTrue(files.certfile)
        self.assertTrue(files.keyfile)

    def test_enabled_tls_with_empty_paths_raises_runtime_error_with_markers(self):
        with self.assertRaises(RuntimeError) as ctx:
            tls.resolve_tls_server_files(
                use_self_signed=True,
                certfile=None,
                keyfile="",
                component_name="API",
            )

        message = str(ctx.exception)
        self.assertIn("<empty certfile>", message)
        self.assertIn("<empty keyfile>", message)
        self.assertNotIn("TypeError", message)


if __name__ == "__main__":
    unittest.main()
