from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def test_run_server_forwards_state_bridge_args_to_bot(tmp_path):
    source = Path(__file__).resolve().parents[1]
    atc_dir = tmp_path / "atc"
    atc_dir.mkdir()
    shutil.copy2(source / "run_server.sh", atc_dir / "run_server.sh")

    capture = tmp_path / "bot-args.txt"
    start_bot = atc_dir / "start_bot.sh"
    start_bot.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$CAPTURE_BOT_ARGS\"\n"
    )
    start_bot.chmod(0o755)

    env = os.environ.copy()
    env.update({"ATC_VIEWER": "0", "CAPTURE_BOT_ARGS": str(capture)})
    subprocess.run(
        ["bash", str(atc_dir / "run_server.sh"), "--state-host", "dcs-server",
         "--state-port", "10309"],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    args = capture.read_text().splitlines()
    assert args[:4] == ["--state-host", "dcs-server", "--state-port", "10309"]
