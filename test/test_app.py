import base64
import os
from fastapi.testclient import TestClient
from workflow_ocr_backend.app import APP
from dotenv import load_dotenv

# Define environemnt variables in ".env" file
load_dotenv(override=True)

headers = {
    'AA-VERSION': os.getenv('AA_VERSION'),
    'EX-APP-ID': os.getenv('APP_ID'),
    'EX-APP-VERSION': os.getenv('APP_VERSION'),
    'AUTHORIZATION-APP-API': base64.b64encode(('testuser:' + os.getenv('APP_SECRET')).encode())
}

# Note: all tests are using the TestClient in a "with"-statement, to ensure lifespan methods
# are triggered. See https://stackoverflow.com/questions/75714883/how-to-test-a-fastapi-endpoint-that-uses-lifespan-function

def test_process_ocr():
    current_dir = os.path.dirname(__file__)
    file_name = "document-ready-for-ocr.pdf"
    ocr_content = "This document is ready for OCR\n"
    with open(f"{current_dir}/testdata/{file_name}", "rb") as file, TestClient(APP, headers=headers) as client:
        response = client.post(
            "/process_ocr",
            files={"file": (file_name, file, "application/pdf")},
            data={"ocrmypdf_parameters": "--skip-text --tesseract-pagesegmode 7 --language eng"}
        )
    assert response.status_code == 200
    response_json = response.json()
    assert "recognizedText" in response_json
    assert response_json["recognizedText"] == ocr_content

def test_process_ocr_image():
    current_dir = os.path.dirname(__file__)
    file_name = "document-image.jpg"
    ocr_content = "Hello from image\n"
    with open(f"{current_dir}/testdata/{file_name}", "rb") as file, TestClient(APP, headers=headers) as client:
        response = client.post(
            "/process_ocr",
            files={"file": (file_name, file, "application/pdf")},
            data={"ocrmypdf_parameters": "--image-dpi 300 --language eng+deu"}
        )
    assert response.status_code == 200
    response_json = response.json()
    assert "recognizedText" in response_json
    assert response_json["recognizedText"] == ocr_content

def test_process_ocr_error_already_processed_file():
    current_dir = os.path.dirname(__file__)
    file_name = "document-already-processed.pdf"
    with open(f"{current_dir}/testdata/{file_name}", "rb") as file, TestClient(APP, headers=headers, raise_server_exceptions=False) as client:
        response = client.post(
            "/process_ocr",
            files={"file": (file_name, file, "application/pdf")}
        )
    assert response.status_code == 500
    response_json = response.json()
    assert "message" in response_json
    assert response_json["message"] == "page already has text! - aborting (use --force-ocr or --mode force to force OCR; see also help for --skip-text, --redo-ocr, and --mode) (PriorOcrFoundError)"
    assert "ocrMyPdfExitCode" in response_json
    assert response_json["ocrMyPdfExitCode"] == 6

def test_process_ocr_error_invalid_file():
    current_dir = os.path.dirname(__file__)
    file_name = "document-invalid.pdf"
    with open(f"{current_dir}/testdata/{file_name}", "rb") as file, TestClient(APP, headers=headers, raise_server_exceptions=False) as client:
        response = client.post(
            "/process_ocr",
            files={"file": (file_name, file, "application/pdf")}
        )
    assert response.status_code == 500
    response_json = response.json()
    assert "message" in response_json
    assert response_json["message"] == " (UnsupportedImageFormatError)"
    assert "ocrMyPdfExitCode" in response_json
    assert response_json["ocrMyPdfExitCode"] == 2

def test_process_ocr_rejects_plugin_parameter(tmp_path):
    # The "plugins" parameter would make OCRmyPDF load and execute an arbitrary
    # Python file => must be rejected before ocrmypdf.ocr() is called.
    current_dir = os.path.dirname(__file__)
    file_name = "document-ready-for-ocr.pdf"
    marker = tmp_path / "pwned"
    plugin = tmp_path / "evil_plugin.py"
    plugin.write_text(f"open({str(marker)!r}, 'w').write('pwned')\n")
    with open(f"{current_dir}/testdata/{file_name}", "rb") as file, TestClient(APP, headers=headers, raise_server_exceptions=False) as client:
        response = client.post(
            "/process_ocr",
            files={"file": (file_name, file, "application/pdf")},
            data={"ocrmypdf_parameters": f"--skip-text --plugins {plugin}"}
        )
    assert response.status_code == 400
    assert response.json()["message"].startswith("Parameter 'plugins' is not allowed")
    assert not marker.exists()

def test_process_ocr_rejects_injected_language():
    current_dir = os.path.dirname(__file__)
    file_name = "document-ready-for-ocr.pdf"
    with open(f"{current_dir}/testdata/{file_name}", "rb") as file, TestClient(APP, headers=headers, raise_server_exceptions=False) as client:
        response = client.post(
            "/process_ocr",
            files={"file": (file_name, file, "application/pdf")},
            data={"ocrmypdf_parameters": "--skip-text --language eng+$(id)"}
        )
    assert response.status_code == 400
    assert response.json()["message"].startswith("Invalid language value '$(id)'")

def test_installed_languages():
    with TestClient(APP, headers=headers) as client:
        response = client.get("/installed_languages")
    assert response.status_code == 200
    response_json = response.json()
    assert isinstance(response_json, list)
    assert "deu" in response_json

def test_enabled_handler():
    with TestClient(APP, headers=headers) as client:
        response = client.put("/enabled?enabled=1")
    assert response.status_code == 200
    response_json = response.json()
    assert "error" in response_json
    assert response_json["error"] == ""