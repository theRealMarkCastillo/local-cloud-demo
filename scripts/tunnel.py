"""Start ngrok for this project's local app port; never expose admin services."""
from pathlib import Path
import argparse
import os

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = '8080'


def web_port():
    values = {}
    environment = ROOT / '.env'
    if environment.exists():
        for line in environment.read_text().splitlines():
            key, separator, value = line.partition('=')
            if separator and key.strip() == 'WEB_PORT':
                values['WEB_PORT'] = value.strip().strip('"').strip("'")
    port = values.get('WEB_PORT', DEFAULT_PORT)
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise SystemExit('WEB_PORT must be an integer from 1 to 65535')
    return str(int(port))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Print only the local forwarding command')
    args = parser.parse_args()
    command = ['ngrok', 'http', 'http://127.0.0.1:' + web_port()]
    if args.dry_run:
        print(' '.join(command))
    else:
        try:
            os.execvp(command[0], command)
        except FileNotFoundError:
            raise SystemExit('Install/authenticate the ngrok CLI before running make tunnel') from None
