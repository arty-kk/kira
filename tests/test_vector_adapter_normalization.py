import math

import pytest

from app.core.vector_adapter import adapt_vector_for_storage, normalize_vector_for_pg


def test_normalize_vector_for_pg_l2_normalize():
    vec = normalize_vector_for_pg([3.0, 4.0], expected_dim=2, l2_normalize=True)
    assert pytest.approx(vec, rel=1e-6) == [0.6, 0.8]
    assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, rel_tol=1e-6)


def test_adapt_vector_for_storage_l2_normalize():
    hv = adapt_vector_for_storage([0.0, 5.0], expected_dim=2, l2_normalize=True)
    assert pytest.approx(hv.to_list(), rel=1e-6) == [0.0, 1.0]
