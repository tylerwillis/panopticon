"""Write-token access to framework-provided documentation routes."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from panopticon.taskservice.__main__ import build_app


def test_write_token_reaches_framework_documentation_routes(tmp_path: Path) -> None:
    # 2119: REQ-035.5.1
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "auth.json").write_text(
        json.dumps({"read": ["read-token-long"], "write": ["write-token-long"]})
    )
    app = build_app(
        db="sqlite://",
        artifacts_root=str(tmp_path / "artifacts"),
        auth_file="auth.json",
        auth_mode="enforced",
        secrets_dir=secrets,
        _home_workflows=tmp_path / "workflows",
    )
    with TestClient(app) as client:
        for path in ["/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"]:
            for method in ["GET", "HEAD"]:
                response = client.request(
                    method, path, headers={"Authorization": "Bearer write-token-long"}
                )
                assert response.status_code != 401, (method, path, response.text)
