"""The release verifier must never treat arbitrary errors as absence."""
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from plane_api.client import PlaneProviderRequestError


@pytest.mark.parametrize("status", [404, 401, 403, 500, None, 200])
def test_canary_reconciliation_requires_typed_404(tmp_path, monkeypatch, status):
    path = Path(__file__).parents[1] / "scripts/reconcile_canary_absence.py"
    spec = importlib.util.spec_from_file_location("canary_reconciliation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source.json"
    output = tmp_path / "result.json"
    original: dict[str, Any] = {step: {"mutation_applied": True, "readback_verified": True}
                for step in ("create", "state_patch", "comment", "delete")}
    original.update(workspace_slug="test", project_id="project", status="FAIL")
    original["create"]["provider_id"] = "target"
    source.write_text(json.dumps(original))
    before = source.read_bytes()

    class FakeClient:
        def get_work_item(self, workspace, project, target):
            assert (workspace, project, target) == ("test", "project", "target")
            if status != 200:
                raise PlaneProviderRequestError(phase="read", http_status=status,
                                                transport_class="http", deterministic=True, detail={})
            return {"id": target}

    monkeypatch.setattr(module, "_load_project_env", lambda: None)
    monkeypatch.setattr(module, "_client", FakeClient)
    monkeypatch.setattr(sys, "argv", [str(path), str(source), str(output)])
    if status == 404:
        module.main()
        assert json.loads(output.read_text())["absence_http_status"] == 404
    else:
        with pytest.raises(SystemExit):
            module.main()
        assert not output.exists()
    assert source.read_bytes() == before
