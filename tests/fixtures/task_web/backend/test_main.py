from fastapi.testclient import TestClient
import main


def test_existing_crud(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test.db")
    main.initialize()
    client = TestClient(main.app)
    created = client.post("/api/tasks", json={"title": "Original"})
    assert created.status_code == 200
    task_id = created.json()["id"]
    updated = client.put(f"/api/tasks/{task_id}", json={"title": "Updated"})
    assert updated.status_code == 200
    assert updated.json()["title"] == "Updated"
    assert client.get("/api/tasks").json()[0]["title"] == "Updated"
    assert client.delete(f"/api/tasks/{task_id}").status_code == 200
    assert client.get("/api/tasks").json() == []
    assert client.put("/api/tasks/999", json={"title": "Missing"}).status_code == 404
