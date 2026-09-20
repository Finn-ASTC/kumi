"""Deterministic money-project worker for explicit --fixture labs; never a model."""
import json
import os
from pathlib import Path
import sys
import termios
import time
import tty

SCRIPTS = Path(__file__).resolve().parents[2]/'skills/agent-orchestrator/scripts'
sys.path.insert(0,str(SCRIPTS))
import protocol


def main():
    runner_path, name = Path(sys.argv[1]), sys.argv[2]
    config = protocol.read_json(runner_path)
    protocol.require(config['fixture'] is True,'fixture-only peer')
    root = runner_path.parent
    cwd = Path(config['targets'][name]['cwd'])
    saved = termios.tcgetattr(sys.stdin.fileno())
    try:
        tty.setraw(sys.stdin.fileno())
        (root/'native'/(name+'.ready')).write_text('deterministic fixture ready')
        print('Fixture ready\r',flush=True)
        line, pending = bytearray(), None
        while True:
            char = os.read(sys.stdin.fileno(),1)
            if not char:
                return
            if char not in (b'\r',b'\n'):
                line.extend(char)
                continue
            text = line.decode('utf-8'); line.clear()
            if text == '/exit':
                return
            if not text:
                continue
            if text == 'ALLOW' and pending is not None:
                request, pending = pending, None
            else:
                envelope = json.loads(text.split('REQUEST=',1)[1])
                request = protocol.load_request(Path(envelope['result_path']).with_name('request.json'))
                protocol.require(request == envelope,'input differs from original request')
                with (root/'native'/(name+'.received.jsonl')).open('a') as stream:
                    stream.write(json.dumps({k:request[k] for k in ('job_id','round_id')})+'\n')
                if name == 'verifier':
                    pending = request
                    print('\x1b[2J\x1b[HCommand: fixture-only permission\r\nAllow once / Deny\r',flush=True)
                    continue
            # Controller makes independent progress while both tasks are outstanding.
            deadline = time.monotonic()+5
            while not (root/'controller-work.json').exists():
                if time.monotonic() > deadline:
                    raise RuntimeError('controller work evidence missing')
                time.sleep(0.01)
            response = {k:request[k] for k in protocol.IDENTITY_FIELDS}
            blocked = 'ASK_OUTPUT_FORMAT' in request['task']
            created, modified = [], []
            if name == 'verifier':
                (cwd/'cases.json').write_text(json.dumps([['0.29',29],['-0.29',-29],['1.10',110]]))
                created = ['cases.json']
            elif 'FIX_DECIMAL' in request['task']:
                (cwd/'money.py').write_text('from decimal import Decimal\ndef cents(value):\n    return int(Decimal(value) * 100)\n')
                modified = ['money.py']
            response.update(status='blocked' if blocked else 'success',output='Deterministic fixture response',
                error=None,blocked_reason='Choose JSON or CSV?' if blocked else None,
                completed_at=protocol.utc_now(),files_created=created,files_modified=modified,
                files_generated=[],files_deleted=[])
            protocol.validate_result(request,response)
            protocol.publish(request['result_path'],response)
            print('\x1b[2J\x1b[HFixture ready\r',flush=True)
    finally:
        termios.tcsetattr(sys.stdin.fileno(),termios.TCSANOW,saved)


if __name__ == '__main__':
    main()
