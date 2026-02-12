import importlib.util
import pathlib
import sys
import tempfile
import types
import unittest

import numpy as np


def _load_api_kb_proc():
    fake_app = types.ModuleType("app")
    fake_config = types.ModuleType("app.config")
    fake_core = types.ModuleType("app.core")
    fake_db = types.ModuleType("app.core.db")
    fake_models = types.ModuleType("app.core.models")
    fake_services = types.ModuleType("app.services")
    fake_responder = types.ModuleType("app.services.responder")
    fake_rag = types.ModuleType("app.services.responder.rag")
    fake_knowledge_proc = types.ModuleType("app.services.responder.rag.knowledge_proc")
    fake_sqlalchemy = types.ModuleType("sqlalchemy")

    fake_app.__path__ = []
    fake_core.__path__ = []
    fake_services.__path__ = []
    fake_responder.__path__ = []
    fake_rag.__path__ = []

    fake_config.settings = types.SimpleNamespace(EMBEDDING_MODEL="test-model", KNOWLEDGE_TOP_K=1)

    class _DummySessionScope:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("DB is unavailable")

    fake_db.session_scope = lambda read_only=True: _DummySessionScope()

    class _ApiKeyKnowledge:
        id = object()
        api_key_id = object()
        status = object()
        embedding_model = object()

    fake_models.ApiKeyKnowledge = _ApiKeyKnowledge

    _tmp_dir = tempfile.TemporaryDirectory()
    fake_knowledge_proc.EMBED_DIR = pathlib.Path(_tmp_dir.name)

    async def _fake_get_query_embedding(_model, _query):
        return np.array([1.0, 0.0], dtype=np.float32)

    def _fake_mmr_select(*, idx_sorted, _scores, _E_cand, k, **_kwargs):
        return list(idx_sorted[:k])

    fake_knowledge_proc._get_query_embedding = _fake_get_query_embedding
    fake_knowledge_proc._mmr_select = _fake_mmr_select

    fake_sqlalchemy.select = lambda *_args, **_kwargs: None

    fake_modules = {
        "app": fake_app,
        "app.config": fake_config,
        "app.core": fake_core,
        "app.core.db": fake_db,
        "app.core.models": fake_models,
        "app.services": fake_services,
        "app.services.responder": fake_responder,
        "app.services.responder.rag": fake_rag,
        "app.services.responder.rag.knowledge_proc": fake_knowledge_proc,
        "sqlalchemy": fake_sqlalchemy,
    }

    original_modules = {name: sys.modules.get(name) for name in fake_modules}
    for name, module in fake_modules.items():
        sys.modules[name] = module

    target_module_name = "app.services.responder.rag.api_kb_proc"
    original_target_module = sys.modules.get(target_module_name)

    module_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "responder" / "rag" / "api_kb_proc.py"
    spec = importlib.util.spec_from_file_location(target_module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[target_module_name] = module
    spec.loader.exec_module(module)

    def _restore_modules() -> None:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

        if original_target_module is None:
            sys.modules.pop(target_module_name, None)
        else:
            sys.modules[target_module_name] = original_target_module

    return module, _tmp_dir, _restore_modules


class ApiKbProcFailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_relevant_for_owner_cache_hit_uses_last_known_good_when_db_unavailable(self):
        api_kb_proc, tmp_dir, restore_modules = _load_api_kb_proc()
        self.addCleanup(tmp_dir.cleanup)
        self.addCleanup(restore_modules)

        owner_id = 501
        model = "fail-closed-model"
        key = (owner_id, model)

        npz_path = api_kb_proc._npz_path(owner_id, model)
        npz_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            npz_path,
            E=np.array([[1.0, 0.0]], dtype=np.float32),
            mean=np.array([0.0, 0.0], dtype=np.float32),
            ids=np.array(["doc-1"], dtype=object),
            texts=np.array(["cached text"], dtype=object),
        )
        mtime = npz_path.stat().st_mtime

        api_kb_proc._API_KB_STATE[key] = {
            "mean": np.array([0.0, 0.0], dtype=np.float32),
            "E": np.array([[1.0, 0.0]], dtype=np.float32),
            "ids": ["doc-1"],
            "texts": ["cached text"],
            "_mtime": mtime,
        }

        async def _fake_has_ready_kb(_owner_id: int, _model: str) -> bool:
            raise AssertionError("DB check must not happen on stable cache-hit")

        api_kb_proc._has_ready_kb = _fake_has_ready_kb

        def _fake_mmr_select(E_cand, _scores_cand, *, top_k, lam):
            self.assertEqual(E_cand.shape[0], 1)
            self.assertEqual(top_k, 1)
            self.assertGreaterEqual(lam, 0.0)
            return [0]

        api_kb_proc._mmr_select = _fake_mmr_select

        result = await api_kb_proc.get_relevant_for_owner("query", owner_id=owner_id, model_name=model)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1], "doc-1")
        self.assertIn(key, api_kb_proc._API_KB_STATE)

    async def test_get_relevant_for_owner_reload_fail_closed_clears_cache_when_kb_not_ready(self):
        api_kb_proc, tmp_dir, restore_modules = _load_api_kb_proc()
        self.addCleanup(tmp_dir.cleanup)
        self.addCleanup(restore_modules)

        owner_id = 777
        model = "reload-model"
        key = (owner_id, model)

        npz_path = api_kb_proc._npz_path(owner_id, model)
        npz_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            npz_path,
            E=np.array([[1.0, 0.0]], dtype=np.float32),
            mean=np.array([0.0, 0.0], dtype=np.float32),
            ids=np.array(["doc-1"], dtype=object),
            texts=np.array(["doc text"], dtype=object),
        )

        api_kb_proc._API_KB_STATE[key] = {
            "mean": np.array([0.0, 0.0], dtype=np.float32),
            "E": np.array([[1.0, 0.0]], dtype=np.float32),
            "ids": ["doc-1"],
            "texts": ["old text"],
            "_mtime": -1.0,
        }

        calls = {"count": 0}

        async def _fake_has_ready_kb(_owner_id: int, _model: str) -> bool:
            calls["count"] += 1
            return False

        api_kb_proc._has_ready_kb = _fake_has_ready_kb

        result = await api_kb_proc.get_relevant_for_owner("query", owner_id=owner_id, model_name=model)

        self.assertEqual(result, [])
        self.assertEqual(calls["count"], 1)
        self.assertNotIn(key, api_kb_proc._API_KB_STATE)

    async def test_invalidate_api_kb_cache_owner_scope_clears_runtime_state(self):
        api_kb_proc, tmp_dir, restore_modules = _load_api_kb_proc()
        self.addCleanup(tmp_dir.cleanup)
        self.addCleanup(restore_modules)

        api_kb_proc._API_KB_STATE[(10, "m1")] = {"_mtime": 1.0}
        api_kb_proc._API_KB_STATE[(10, "m2")] = {"_mtime": 2.0}
        api_kb_proc._API_KB_STATE[(20, "m1")] = {"_mtime": 3.0}

        api_kb_proc.invalidate_api_kb_cache(10)

        self.assertNotIn((10, "m1"), api_kb_proc._API_KB_STATE)
        self.assertNotIn((10, "m2"), api_kb_proc._API_KB_STATE)
        self.assertIn((20, "m1"), api_kb_proc._API_KB_STATE)


if __name__ == "__main__":
    unittest.main()
