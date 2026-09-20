"""Deterministic raw-terminal peer used only by the opt-in tmux tests."""

import importlib.util
import json
import os
from pathlib import Path
import sys
import termios
import tty


def show_permission(step):
    """Render a synthetic Codex-shaped prompt; only this test peer handles ALLOW."""
    prior = '✔ Fixture operation a completed\r\n' if step == 'b' else ''
    print(f'\x1b[2J\x1b[H{prior}Would you like to run the following command?\r\n'
          f'$ fixture-operation {step}\r\n'
          '› 1. Yes, proceed (y)\r\n'
          '  2. No, and tell the fixture what to do differently (esc)\r', flush=True)


def main():
    config = json.loads(Path(__file__).with_suffix(".json").read_text())
    spec = importlib.util.spec_from_file_location("protocol", config["tool"])
    protocol = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(protocol)
    original = termios.tcgetattr(sys.stdin.fileno())
    try:
        tty.setraw(sys.stdin.fileno())
        Path(config["ready"]).write_text("ready")
        line = bytearray()
        pending = None
        permission_stage = None
        repaint = 0
        while True:
            char = os.read(sys.stdin.fileno(), 1)
            if not char:
                return
            if char not in (b"\n", b"\r"):
                line.extend(char)
                continue
            text = line.decode("utf-8")
            line.clear()
            if text == "/exit":
                return
            if not text:
                continue
            if text == 'NEXT_PERMISSION' and permission_stage == 'prose':
                permission_stage = 'a'
                show_permission('a')
                continue
            if text == 'ALLOW' and permission_stage == 'a':
                permission_stage = 'b'
                show_permission('b')
                continue
            if text == "REPAINT" and pending is not None:
                repaint += 1
                print(f"\x1b[2J\x1b[HElapsed {repaint}s\r\nCommand: deterministic fixture operation\r\n"
                      f"Allow once / Deny\r\nElapsed {repaint}s\r", flush=True)
                continue
            if text == "ALLOW" and pending is not None:
                request, pending = pending, None
                permission_stage = None
                print("\x1b[2J\x1b[HFixture permission resolved\r", flush=True)
            else:
                envelope = json.loads(text.split("REQUEST=", 1)[1])
                request = protocol.load_request(Path(envelope["result_path"]).with_name("request.json"))
                if envelope != request:
                    raise ValueError("terminal input did not preserve the prepared request")
                with Path(config["received"]).open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(request, ensure_ascii=True) + "\n")
                if request['task'].startswith('PERMISSIONS:'):
                    pending, permission_stage = request, 'prose'
                    print('\x1b[2J\x1b[HREQUEST: 沙箱拒绝则申请权限；不要自动批准。\r\n'
                          '• We should approve or deny after reviewing each operation.\r\n'
                          'Fixture prose ready\r', flush=True)
                    continue
                if request["task"].startswith("PAUSE:"):
                    pending = request
                    print("\x1b[2J\x1b[HCommand: deterministic fixture operation\r\nAllow once / Deny\r", flush=True)
                    continue
            response = {field: request[field] for field in protocol.IDENTITY_FIELDS}
            blocked = request["task"].startswith("ASK:")
            response.update(status="blocked" if blocked else "success", output=request["task"],
                            error=None, blocked_reason="Which output format?" if blocked else None,
                            completed_at=protocol.utc_now(), **{field: [] for field in protocol.FILE_FIELDS})
            protocol.validate_result(request, response)
            protocol.publish(request["result_path"], response)
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, original)


if __name__ == "__main__":
    main()
