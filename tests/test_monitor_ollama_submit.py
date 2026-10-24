"""Optional native submission uses its explicit public task-admission seam."""
import httpx
import pytest

from cloudyfan_jev.ollama_api import OllamaApp
from test_http_api import service_client


def test_explicit_submit_can_accept_a_ready_task_without_implicit_execution(tmp_path):
    original, service, _, upstream, _ = service_client(tmp_path)
    original.close()
    def submit(principal, payload):
        return service.create(principal, payload)
    app = OllamaApp(service, models={"cloudyfan/arithmetic:v1": "arithmetic"},
                    authenticate=lambda _: "admin", submit_task=submit)
    with httpx.Client(transport=httpx.WSGITransport(app), base_url="http://fixture",
                      headers={"X-CloudyFan-Run-ID": "submitted-ready"}) as client:
        response = client.post("/api/generate", json={"model": "cloudyfan/arithmetic:v1",
                               "prompt": "Return 1+1", "stream": False})
        assert response.status_code == 409
        assert response.json()["cloudyfan"]["status"] == "ready"
        assert client.get("/v1/tasks/submitted-ready").json()["status"] == "ready"
        assert upstream.requests == []


@pytest.mark.parametrize("submit", [False, "not-a-callable"])
def test_explicit_submit_rejects_noncallables_before_requests(tmp_path, submit):
    original, service, _, upstream, _ = service_client(tmp_path)
    original.close()
    with pytest.raises(ValueError, match="^Explicit task submission must be callable$"):
        OllamaApp(service, models={"cloudyfan/arithmetic:v1": "arithmetic"},
                  authenticate=lambda _: "admin", submit_task=submit)
    assert upstream.requests == []
