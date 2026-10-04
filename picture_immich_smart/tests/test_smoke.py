"""picture_immich_smart smoke: renders at every declared size with no network call."""

from __future__ import annotations

import pytest
from flask.testing import FlaskClient


@pytest.mark.parametrize("size", ["sm", "md", "lg"])
def test_picture_immich_smart_renders(client: FlaskClient, size: str) -> None:
    resp = client.get(f"/_test/render?plugin=picture_immich_smart&size={size}")
    assert resp.status_code == 200
