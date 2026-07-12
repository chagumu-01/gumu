import subprocess
import sys
from pathlib import Path

import mini_gumu


def test_mini_gumu_module_and_public_exports():
    assert mini_gumu.Gumu is not None
    assert mini_gumu.FakeModelClient is not None
    assert not hasattr(mini_gumu, "MiniAgent")
    result = subprocess.run([sys.executable, "-m", "mini_gumu", "--help"], capture_output=True, text=True, check=True)
    assert "Teaching-sized Gumu agent harness" in result.stdout


def test_readme_main_mapping_points_to_existing_files():
    repo_root = Path(__file__).resolve().parents[3]
    main_files = [
        "gumu/cli.py",
        "gumu/runtime.py",
        "gumu/agent_loop.py",
        "gumu/context_manager.py",
        "gumu/providers/clients.py",
        "gumu/tool_executor.py",
        "gumu/tools.py",
        "gumu/task_state.py",
        "gumu/run_store.py",
        "gumu/workspace.py",
    ]
    for path in main_files:
        assert (repo_root / path).exists()
