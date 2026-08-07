"""Opt-in live acceptance for pi against a host-local OpenAI-compatible model.

The normal suite never calls a model. Run this inside a task container with
``PANOPTICON_PI_LOCAL_ACCEPTANCE=1`` and a server listening at
``PANOPTICON_PI_LOCAL_MODEL_URL`` (normally ``http://127.0.0.1:18000/v1`` on the host).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from panopticon.harnesses import BootstrapContext
from panopticon.harnesses.pi import PiHarness

_LIVE_KEYS = (
    "PANOPTICON_PI_LOCAL_MODEL_URL",
    "PANOPTICON_PI_LOCAL_MODEL_ID",
    "PANOPTICON_PI_LOCAL_PROVIDER",
)


def _live_configured(environ: Mapping[str, str]) -> bool:
    if environ.get("PANOPTICON_PI_LOCAL_ACCEPTANCE") != "1" or not all(
        environ.get(key) for key in _LIVE_KEYS
    ):
        return False
    parsed = urlsplit(environ["PANOPTICON_PI_LOCAL_MODEL_URL"])
    return parsed.scheme in {"http", "https"} and parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


_LIVE = _live_configured(os.environ)


# 2119: REQ-051.3.4
def test_complete_live_configuration_enables_acceptance() -> None:
    assert (
        _live_configured(
            {
                "PANOPTICON_PI_LOCAL_ACCEPTANCE": "1",
                "PANOPTICON_PI_LOCAL_MODEL_URL": "http://127.0.0.1:18000/v1",
                "PANOPTICON_PI_LOCAL_MODEL_ID": "laguna-s-2.1-nvfp4",
                "PANOPTICON_PI_LOCAL_PROVIDER": "sparky2-vllm",
            }
        )
        is True
    )


# 2119: REQ-051.3.4
@pytest.mark.parametrize(
    "missing",
    [
        "PANOPTICON_PI_LOCAL_ACCEPTANCE",
        "PANOPTICON_PI_LOCAL_MODEL_URL",
        "PANOPTICON_PI_LOCAL_MODEL_ID",
        "PANOPTICON_PI_LOCAL_PROVIDER",
    ],
)
def test_live_local_turn_requires_every_explicit_configuration_value(missing: str) -> None:
    configured = {
        "PANOPTICON_PI_LOCAL_ACCEPTANCE": "1",
        "PANOPTICON_PI_LOCAL_MODEL_URL": "http://127.0.0.1:18000/v1",
        "PANOPTICON_PI_LOCAL_MODEL_ID": "laguna-s-2.1-nvfp4",
        "PANOPTICON_PI_LOCAL_PROVIDER": "sparky2-vllm",
    }
    configured.pop(missing)

    assert _live_configured(configured) is False


# 2119: REQ-051.3.4
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PANOPTICON_PI_LOCAL_ACCEPTANCE", "true"),
        ("PANOPTICON_PI_LOCAL_ACCEPTANCE", "0"),
        ("PANOPTICON_PI_LOCAL_MODEL_URL", ""),
        ("PANOPTICON_PI_LOCAL_MODEL_ID", ""),
        ("PANOPTICON_PI_LOCAL_PROVIDER", ""),
        ("PANOPTICON_PI_LOCAL_MODEL_URL", "https://models.example/v1"),
    ],
)
def test_live_local_turn_rejects_wrong_opt_in_or_empty_configuration(key: str, value: str) -> None:
    configured = {
        "PANOPTICON_PI_LOCAL_ACCEPTANCE": "1",
        "PANOPTICON_PI_LOCAL_MODEL_URL": "http://127.0.0.1:18000/v1",
        "PANOPTICON_PI_LOCAL_MODEL_ID": "laguna-s-2.1-nvfp4",
        "PANOPTICON_PI_LOCAL_PROVIDER": "sparky2-vllm",
    }
    configured[key] = value

    assert _live_configured(configured) is False


def _run_live_local_turn(tmp_path: Path) -> None:
    if shutil.which("pi") is None:
        pytest.fail("pi executable is required for PANOPTICON_PI_LOCAL_ACCEPTANCE=1")
    source_url = os.environ["PANOPTICON_PI_LOCAL_MODEL_URL"]
    model_id = os.environ["PANOPTICON_PI_LOCAL_MODEL_ID"]
    provider = os.environ["PANOPTICON_PI_LOCAL_PROVIDER"]
    assert source_url.startswith(("http://127.0.0.1:", "http://localhost:", "http://[::1]:"))
    credentials = tmp_path / "credentials"
    source_agent = credentials / "pi" / "agent"
    source_agent.mkdir(parents=True)
    (source_agent / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    provider: {
                        "baseUrl": source_url,
                        "api": "openai-completions",
                        "apiKey": "local",
                        "models": [{"id": model_id}],
                    }
                }
            }
        )
    )
    harness = PiHarness()
    harness.bootstrap(
        BootstrapContext(
            home=tmp_path,
            cwd=Path("/workspace"),
            service_url="http://host.docker.internal:8000",
            task_id="live-pi-local",
            environ={"PANOPTICON_CREDENTIALS": str(credentials)},
        )
    )
    rendered = json.loads((tmp_path / ".pi" / "agent" / "models.json").read_text())
    parsed_source = urlsplit(source_url)
    expected_url = urlunsplit(
        parsed_source._replace(netloc=f"host.docker.internal:{parsed_source.port}")
    )
    assert rendered["providers"][provider]["baseUrl"] == expected_url
    request_log = tmp_path / "pi-requests.log"
    fetch_probe = tmp_path / "observe-pi-fetch.cjs"
    fetch_probe.write_text(
        """
const fs = require("fs");
const originalFetch = globalThis.fetch;
globalThis.fetch = async function(input, init) {
  const url = typeof input === "string" ? input : input.url;
  fs.appendFileSync(process.env.PANOPTICON_PI_REQUEST_LOG, `${url}\n`);
  return originalFetch.call(this, input, init);
};
""".lstrip()
    )

    completed = subprocess.run(
        [
            "pi",
            "--mode",
            "json",
            "--no-session",
            "--model",
            f"{provider}/{model_id}",
            "Reply with exactly LOCAL_PI_RESPONSE_7F3A",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
        env={
            **os.environ,
            "PI_CODING_AGENT_DIR": str(tmp_path / ".pi" / "agent"),
            "PANOPTICON_PI_REQUEST_LOG": str(request_log),
            "NODE_OPTIONS": " ".join(
                filter(
                    None,
                    [os.environ.get("NODE_OPTIONS"), f"--require={fetch_probe}"],
                )
            ),
        },
    )

    observed_urls = request_log.read_text().splitlines()
    assert any(
        url == expected_url or url.startswith(f"{expected_url.rstrip('/')}/")
        for url in observed_urls
    )
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    assistant_messages = [
        event["message"]
        for event in events
        if event.get("type") == "message_end"
        and event.get("message", {}).get("role") == "assistant"
    ]
    assert len(assistant_messages) == 1
    text_parts = [
        part["text"] for part in assistant_messages[0]["content"] if part.get("type") == "text"
    ]
    assert "".join(text_parts).strip() == "LOCAL_PI_RESPONSE_7F3A"


# 2119: REQ-051.3.4
def test_pi_completes_a_turn_through_rewritten_host_loopback(tmp_path: Path) -> None:
    if not _LIVE:
        pytest.skip("set the pi local acceptance flag, loopback URL, provider, and model id")
    _run_live_local_turn(tmp_path)


# 2119: REQ-051.3.4
def test_complete_live_configuration_enters_the_real_turn_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered: list[Path] = []
    monkeypatch.setitem(globals(), "_LIVE", True)
    monkeypatch.setitem(globals(), "_run_live_local_turn", entered.append)

    test_pi_completes_a_turn_through_rewritten_host_loopback(tmp_path)

    assert entered == [tmp_path]
