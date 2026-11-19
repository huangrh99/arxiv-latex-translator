import json
import threading
from http.client import HTTPConnection
from pathlib import Path

from arxiv_latex_translator.app import create_server
from arxiv_latex_translator.pipeline import Job, JobManager


def test_health_and_invalid_job_request(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    manager._jobs["2401.12345-history"] = Job(
        id="2401.12345-history",
        source="https://arxiv.org/abs/2401.12345",
        arxiv_id="2401.12345",
        status="completed",
    )
    server = create_server("127.0.0.1", 0, manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_address[1])
    connection.request("GET", "/")
    response = connection.getresponse()
    assert response.status == 200
    assert b"ArXiv LaTeX Translator" in response.read()
    connection.request("GET", "/compare.html")
    response = connection.getresponse()
    assert response.status == 200
    assert b"Arxiv Translation" in response.read()
    for asset in ("/config.js", "/formatters.js", "/pdf-viewer.js"):
        connection.request("GET", asset)
        response = connection.getresponse()
        assert response.status == 200
        assert response.read()
    connection.request("GET", "/healthz")
    response = connection.getresponse()
    assert response.status == 200
    assert json.loads(response.read()) == {"status": "ok"}
    connection.request("GET", "/api/status")
    response = connection.getresponse()
    status_payload = json.loads(response.read())
    assert response.status == 200
    assert status_payload["codex_cli"]["status"] == "checking"
    connection.request("OPTIONS", "/api/jobs", headers={"Origin": "null", "Access-Control-Request-Method": "POST"})
    response = connection.getresponse()
    assert response.status == 204
    assert response.getheader("Access-Control-Allow-Origin") == "*"
    connection.request("GET", "/api/jobs?query=2401.12345")
    response = connection.getresponse()
    payload = json.loads(response.read())
    assert response.status == 200
    assert payload["count"] == 1
    assert payload["jobs"][0]["arxiv_id"] == "2401.12345"
    assert payload["jobs"][0]["original_pdf_url"] == "https://arxiv.org/pdf/2401.12345"
    connection.request("POST", "/api/jobs", body=json.dumps({"source": "bad"}), headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    assert response.status == 400
    connection.request("POST", "/api/jobs", body="[]", headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    assert response.status == 400
    server.shutdown()
    server.server_close()
