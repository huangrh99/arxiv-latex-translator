import json
import threading
from http.client import HTTPConnection
from pathlib import Path

from arxiv_latex_translator.app import create_server
from arxiv_latex_translator.pipeline import JobManager


def test_health_and_invalid_job_request(tmp_path: Path) -> None:
    server = create_server("127.0.0.1", 0, JobManager(tmp_path / "jobs"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_address[1])
    connection.request("GET", "/")
    response = connection.getresponse()
    assert response.status == 200
    assert b"ArXiv LaTeX Translator" in response.read()
    connection.request("GET", "/healthz")
    response = connection.getresponse()
    assert response.status == 200
    assert json.loads(response.read()) == {"status": "ok"}
    connection.request("POST", "/api/jobs", body=json.dumps({"source": "bad"}), headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    assert response.status == 400
    connection.request("POST", "/api/jobs", body="[]", headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    assert response.status == 400
    server.shutdown()
    server.server_close()
