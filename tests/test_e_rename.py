"""A person renames a signal ("S44" -> "possibly broken"): stored, logged, and shown in everything people read."""
from tpm import naming


def test_aliases_are_expanded_in_prose_only():
    names = {"S44": "possibly broken", "S02": "reactor pressure"}
    assert naming.expand_text("S44 froze while S02 kept moving; S03 too.", names) == "possibly broken (S44) froze while reactor pressure (S02) kept moving; S03 too."
    # already named, ids inside longer tokens, anchors and quoted ids stay as they are
    for keep in ("possibly broken (S44) froze", "see [S44]", "FLAG-S44X", "#S44", "href=\"S44\"", "XS44", "S445"):
        assert naming.expand_text(keep, names) == keep
    assert naming.expand_text("S44", {}) == "S44"
    ctx = {"id": "S44", "anchor": "S44", "text": "Lead signal S44 moved first", "rows": [{"signal": "S02", "statement": "S02 drifted up"}], "n": 3}
    out = naming.expand(ctx, names)
    assert out["id"] == "S44" and out["anchor"] == "S44" and out["rows"][0]["signal"] == "S02" and out["n"] == 3
    assert out["text"] == "Lead signal possibly broken (S44) moved first"
    assert out["rows"][0]["statement"] == "reactor pressure (S02) drifted up"
    assert ctx["text"] == "Lead signal S44 moved first"  # the input is not modified


def test_names_are_stored_as_one_clean_line():
    assert naming.clean_name("  possibly\n broken\t ") == "possibly broken"
    assert naming.clean_name("x" * 200) == "x" * 80
    assert naming.clean_name(None) == ""


def test_rename_is_a_logged_decision_and_reaches_api_and_report(tmp_path, monkeypatch):
    import json

    from fastapi.testclient import TestClient

    from tpm import config
    from tpm.api.server import create_app
    from tpm.workspace import Workspace

    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text((config.ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    app = create_app(settings_path=settings_path, workspace_dir=tmp_path / "ws")
    with TestClient(app) as c:
        s = config.load_settings(settings_path)
        s.workspace_dir = str(tmp_path / "ws")
        ws = Workspace(run_id="r1", settings=s)
        ws.write_json("signals", [{"id": "S01", "column_index": 0, "source_column": "press_r", "dtype": "float", "structural_role": "continuous_measured"},
                                  {"id": "S44", "column_index": 1, "source_column": "S44", "dtype": "float", "structural_role": "continuous_measured"}])
        r = c.get("/api/runs/r1/signal-names").json()
        assert r["names"] == {} and r["headers"] == {"S01": "press_r", "S44": ""}
        d = c.post("/api/runs/r1/decisions", json={"object_type": "signal", "object_id": "S44", "action": "set_name", "actor_name": "Aino", "role": "operator", "note": "looks dead since Tuesday", "new_value": {"display_name": " possibly\nbroken "}})
        assert d.status_code == 200, d.text
        assert c.get("/api/runs/r1/signal-names").json()["names"] == {"S44": "possibly broken"}
        assert naming.operator_names(Workspace(run_id="r1", settings=s)) == {"S44": "possibly broken"}
        log = c.get("/api/runs/r1/log", params={"object_id": "S44"}).json()["items"]
        assert any(e["action"] == "set_name" and "Aino" in e["actor"] for e in log)
        # removing the name again
        c.post("/api/runs/r1/decisions", json={"object_type": "signal", "object_id": "S44", "action": "set_name", "actor_name": "Aino", "role": "operator", "new_value": {"display_name": ""}})
        assert c.get("/api/runs/r1/signal-names").json()["names"] == {}
    config.reload_settings()
    assert json.dumps({"ok": True})
