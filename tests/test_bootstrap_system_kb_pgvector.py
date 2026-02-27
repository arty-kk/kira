import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "bootstrap_system_kb_pgvector.py"


def _load_script_module(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location("bootstrap_system_kb_pgvector_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_load_items_deduplicates_tags_and_logs_warning(tmp_path, monkeypatch, caplog):
    module = _load_script_module(monkeypatch)
    kb_path = tmp_path / "kb.json"
    kb_path.write_text(
        '[{"id":"1","text":"  hello ","tags":[" foo ","foo",""," bar ","foo","bar"," "]}]',
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        items, stats = module._load_items(kb_path)

    assert items == [{"id": "1", "text": "hello", "tags": ["foo", "bar"]}]
    assert stats == {"items_with_duplicate_tags": 1, "duplicates_removed_total": 3}
    assert "KB tag duplicates removed during load" in caplog.text


def test_embedding_param_l2_normalizes(monkeypatch):
    module = _load_script_module(monkeypatch)

    hv = module._embedding_param([3.0, 4.0], expected_dim=2, model="text-embedding-3-large")

    assert hv.to_list() == pytest.approx([0.6, 0.8], rel=1e-3)
