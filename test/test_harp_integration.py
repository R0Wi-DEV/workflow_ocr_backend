
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterable

import json
import httpx
import pytest
from dotenv import dotenv_values
import logging

pytestmark = [
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI not available"),
    pytest.mark.harp_integration,
]

HARP_IMAGE = os.environ.get("HARP_TEST_IMAGE", "ghcr.io/nextcloud/nextcloud-appapi-harp:release")
LOCAL_DOCKER_ENGINE_PORT = 24000
AGENT_TIMEOUT = 180
CONTAINER_TIMEOUT = 180


def test_harp_deploys_and_configures_frp(tmp_path):
    repo_root = Path(__file__).resolve().parent.parent
    env_vars = dotenv_values()
    required_keys = ["APP_ID", "APP_PORT", "APP_VERSION", "APP_SECRET", "AA_VERSION"]
    missing = [key for key in required_keys if not env_vars.get(key)]
    if missing:
        pytest.skip(f"Missing required keys in .env for HaRP test: {', '.join(missing)}")

    shared_key = f"harp-key-{uuid.uuid4().hex}"
    harp_container = f"harp-integration-{uuid.uuid4().hex[:8]}"
    network_name = f"harp-net-{uuid.uuid4().hex[:8]}"
    image_tag = f"workflow-ocr-backend-harp:{uuid.uuid4().hex}"
    instance_id = f"it{uuid.uuid4().hex[:8]}"
    exapp_name = env_vars["APP_ID"]
    exapp_container = f"nc_app_{instance_id}_{exapp_name}"
    volume_name = f"{exapp_container}_data"
    exapps_port = _reserve_host_port()
    frp_port = _reserve_host_port()
    cert_dir = tmp_path / "harp-certs"
    cert_dir.mkdir(parents=True, exist_ok=True)

    app_env = {key: value for key, value in env_vars.items() if value is not None}
    app_env.update(
        {
            "HP_SHARED_KEY": shared_key,
            "HP_FRP_ADDRESS": f"{harp_container}:8782",
            "HP_FRP_PORT": "8782",
            "HP_FRP_DISABLE_TLS": "false",
        }
    )

    network_created = False
    harp_started = False
    http_client: httpx.Client | None = None
    try:
        _build_app_image(repo_root, image_tag)
        _run_command(["docker", "network", "create", network_name], check=True)
        network_created = True

        # Create HaRP container
        harp_run_cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            harp_container,
            "--hostname",
            harp_container,
            "--network",
            network_name,
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            f"{cert_dir}:/certs",
            "-e",
            f"HP_SHARED_KEY={shared_key}",
            "-e",
            "NC_INSTANCE_URL=http://nextcloud.local",
            "-e",
            "HP_LOG_LEVEL=debug",
            "-e",
            "HP_VERBOSE_START=1",
            "-p",
            f"{exapps_port}:8780",
            "-p",
            f"{frp_port}:8782",
            HARP_IMAGE,
        ]
        _run_command(harp_run_cmd, check=True)
        harp_started = True

        http_client = httpx.Client(timeout=15.0, trust_env=False)
        exapps_base = f"http://127.0.0.1:{exapps_port}"
        agent_headers = {
            "harp-shared-key": shared_key,
            "EX-APP-ID": exapp_name,
            "AUTHORIZATION-APP-API": "test-token",
            "AA-VERSION": env_vars.get("AA_VERSION", "33"),
        }

        # Wait for HaRP agent to be ready via ExApps app_api
        _wait_for_agent_ready_via_exapps(http_client, exapps_base, agent_headers)

        docker_headers = agent_headers | {"docker-engine-port": str(LOCAL_DOCKER_ENGINE_PORT)}
        metadata_payload = {
            "exapp_token": "test-token",
            "exapp_version": env_vars["APP_VERSION"],
            "host": "127.0.0.1",
            "port": int(env_vars["APP_PORT"]),
            "routes": [],
        }

        # Create ExApp storage
        resp = _call_exapps(
            http_client,
            exapps_base,
            "POST",
            f"/exapps/app_api/exapp_storage/{exapp_name}",
            agent_headers,
            json_payload=metadata_payload,
        )
        if resp.status_code != 204:
            pytest.fail(f"Failed to seed ExApp metadata: {resp.status_code} {resp.text}")

        env_list = _format_env(app_env)
        create_payload = {
            "name": exapp_name,
            "instance_id": instance_id,
            "image_id": image_tag,
            "network_mode": network_name,
            "environment_variables": env_list,
            "mount_points": [],
            "resource_limits": {},
            "restart_policy": "no",
        }

        # Create ExApp container
        resp = _call_exapps(http_client, exapps_base, "POST", "/exapps/app_api/docker/exapp/create", docker_headers, json_payload=create_payload, timeout=60.0)
        if resp.status_code != 201:
            harp_logs = docker_logs(harp_container)
            pytest.fail(
                "HaRP failed to create ExApp: "
                f"{resp.status_code} {resp.text}\nHaRP logs:\n{harp_logs}"
            )

        install_payload = {
            "name": exapp_name,
            "instance_id": instance_id,
            "system_certs_bundle": None,
            "install_frp_certs": True,
        }

        # Install certificates in ExApp container
        resp = _call_exapps(http_client, exapps_base, "POST", "/exapps/app_api/docker/exapp/install_certificates", docker_headers, json_payload=install_payload)
        if resp.status_code != 204:
            pytest.fail(f"Failed to install certificates: {resp.status_code} {resp.text}")

        # Start ExApp container
        resp = _call_exapps(http_client, exapps_base, "POST", "/exapps/app_api/docker/exapp/start", docker_headers, json_payload={"name": exapp_name, "instance_id": instance_id})
        if resp.status_code not in (200, 204):
            pytest.fail(f"Failed to start ExApp container: {resp.status_code} {resp.text}")

        # Wait for ExApp to be running
        _wait_for_exapp_start_via_exapps(http_client, exapps_base, docker_headers, exapp_name, instance_id)

        # ASSERTIONS
        logs = docker_logs(exapp_container)
        if "permission denied" in logs.lower():
            pytest.fail(f"Detected permission issue in ExApp logs:\n{logs}")
        assert "Found /certs/frp directory." in logs, f"FRP certificates were not detected in logs:\n{logs}"
        assert "Creating configuration with TLS certificates" in logs

        _assert_cert_files_present(exapp_container)
        config_contents = docker_exec(exapp_container, ["cat", "/frpc.toml"])
        assert 'transport.tls.enable = true' in config_contents
        assert 'transport.tls.certFile = "/certs/frp/client.crt"' in config_contents
        assert f'remotePort = {env_vars["APP_PORT"]}' in config_contents
        assert f'metadatas.token = "{shared_key}"' in config_contents

        # Cleanup ExApp container
        resp = _call_exapps(http_client, exapps_base, "POST", "/exapps/app_api/docker/exapp/remove", docker_headers, json_payload={"name": exapp_name, "instance_id": instance_id, "remove_data": True})
        if resp.status_code != 204:
            pytest.fail(f"Failed to remove ExApp container: {resp.status_code} {resp.text}")

        resp = _call_exapps(http_client, exapps_base, "POST", "/exapps/app_api/docker/exapp/exists", docker_headers, json_payload={"name": exapp_name, "instance_id": instance_id})
        if resp.status_code != 200 or resp.json().get("exists"):
            pytest.fail("ExApp container still exists after removal")

        _run_command(["docker", "volume", "rm", "-f", volume_name])

    finally:
        if http_client is not None:
            http_client.close()
        container_exists = _run_command(
            ["docker", "ps", "-a", "-q", "-f", f"name={exapp_container}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if container_exists:
            app_logs = docker_logs(exapp_container)
            if app_logs:
                _log(f"\n\n#### App Container Logs #### \n\n{app_logs}")
            _run_command(["docker", "rm", "-f", exapp_container])
        if harp_started:
            _run_command(["docker", "rm", "-f", harp_container])
        if network_created:
            _run_command(["docker", "network", "rm", network_name])


def _build_app_image(repo_root: Path, image_tag: str) -> None:
    build_cmd = [
        "docker",
        "build",
        "--target",
        "app",
        "-t",
        image_tag,
        str(repo_root),
    ]
    result = _run_command(build_cmd, capture_output=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"docker build failed: {result.stderr}\n{result.stdout}")


def _run_command(
    args: list[str], *, check: bool = False, capture_output: bool = False, text: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        check=check,
        capture_output=capture_output,
        text=text,
    )

def _reserve_host_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]

def _call_exapps(http_client: httpx.Client, exapps_base: str, method: str, path: str, headers: dict, json_payload=None, timeout: float = 15.0):
    if not path.startswith("/"):
        path = "/" + path
    url = f"{exapps_base}{path}"
    try:
        if method.upper() == "GET":
            return http_client.get(url, headers=headers, timeout=timeout)
        if method.upper() == "POST":
            return http_client.post(url, headers=headers, json=json_payload, timeout=timeout)
        raise ValueError(f"Unsupported method: {method}")
    except httpx.ConnectError:
        raise

def _wait_for_agent_ready_via_exapps(http_client: httpx.Client, exapps_base: str, headers: dict) -> None:
    probe_headers = headers.copy()
    # Ask HaRP to use the local (built-in) Docker Engine remote port.
    probe_headers.setdefault("docker-engine-port", str(LOCAL_DOCKER_ENGINE_PORT))
    # AA-VERSION isn't relevant for app_api ping but keep a sane default
    probe_headers.setdefault("AA-VERSION", "33")

    ping_path = "/exapps/app_api/v1.41/_ping"
    deadline = time.time() + AGENT_TIMEOUT
    while time.time() < deadline:
        try:
            resp = _call_exapps(http_client, exapps_base, "GET", ping_path, probe_headers)
            if resp.status_code == 200:
                # Expect the body to contain 'OK'
                try:
                    body = resp.text.strip()
                except Exception:
                    body = ""
                if body == "OK":
                    return
        except httpx.ConnectError as ce:
            _log(f"ConnectError while probing HaRP readiness via ExApps: {ce}")
        except httpx.ReadError as re:
            _log(f"ReadError while probing HaRP readiness via ExApps: {re}")
        except httpx.RemoteProtocolError as rpe:
            _log(f"RemoteProtocolError while probing HaRP readiness via ExApps: {rpe}")
        time.sleep(1)
    pytest.fail("Timed out waiting for HaRP readiness via ExApps app_api _ping")

def _wait_for_exapp_start_via_exapps(http_client: httpx.Client, exapps_base: str, headers: dict, app_name: str, instance_id: str) -> None:
    payload = {"name": app_name, "instance_id": instance_id}
    probe_headers = headers.copy()
    probe_headers.setdefault("EX-APP-ID", app_name)
    probe_headers.setdefault("AUTHORIZATION-APP-API", "test-token")
    deadline = time.time() + CONTAINER_TIMEOUT
    while time.time() < deadline:
        try:
            resp = _call_exapps(http_client, exapps_base, "POST", "/exapps/app_api/docker/exapp/wait_for_start", probe_headers, json_payload=payload)
            if resp.status_code == 200:
                try:
                    j = resp.json()
                    if j.get("started"):
                        return
                except Exception:
                    pass
        except httpx.ConnectError as ce:
            _log(f"ConnectError while waiting for ExApp start via ExApps: {ce}")
        except httpx.RemoteProtocolError as rpe:
            _log(f"RemoteProtocolError while waiting for ExApp start via ExApps: {rpe}")
        time.sleep(2)
    pytest.fail("ExApp container did not reach running state in time (via ex_apps)")

def _format_env(items: dict[str, str]) -> list[str]:
    formatted: list[str] = []
    for key, value in sorted(items.items()):
        formatted.append(f"{key}={value}")
    return formatted

def docker_logs(container_name: str) -> str:
    proc = _run_command(["docker", "logs", container_name], capture_output=True)
    return proc.stdout + proc.stderr if proc.stderr else proc.stdout

def docker_exec(container_name: str, cmd: Iterable[str]) -> str:
    proc = _run_command(["docker", "exec", container_name, *cmd], capture_output=True)
    if proc.returncode != 0:
        pytest.fail(
            f"docker exec {' '.join(cmd)} failed with rc={proc.returncode}:\nstdout:{proc.stdout}\nstderr:{proc.stderr}"
        )
    return proc.stdout

def _assert_cert_files_present(container_name: str) -> None:
    for filename in ("ca.crt", "client.crt", "client.key"):
        proc = _run_command(
            ["docker", "exec", container_name, "test", "-f", f"/certs/frp/{filename}"], capture_output=True
        )
        if proc.returncode != 0:
            pytest.fail(
                f"Expected certificate {filename} is missing in container {container_name}: {proc.stdout} {proc.stderr}"
            )

def _log(string: str) -> None:
    logger = logging.getLogger(__name__)
    logger.info(string)
