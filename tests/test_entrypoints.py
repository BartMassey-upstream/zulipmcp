import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["zulipmcp", "zulipmcp.mcp"])
def test_module_entrypoint_initializes_server_once(module):
    result = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-m", module],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "RuntimeWarning" not in result.stderr
    assert result.stderr.count("zulipmcp MCP server starting") == 1


def test_package_preserves_lazy_mcp_api():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import zulipmcp; "
                "assert callable(zulipmcp.configure); "
                "assert zulipmcp.mcp.name == 'Zulip Messaging'"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
