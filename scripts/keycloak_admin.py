"""Project-scoped local Keycloak administration with an ephemeral private config."""
from contextlib import contextmanager
from pathlib import Path
import secrets
import subprocess

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def admin():
    config = '/tmp/workbench-admin-' + secrets.token_hex(16) + '.config'
    base = ['docker', 'compose', 'exec', '-T', 'keycloak']
    def run(*args, **kwargs):
        return subprocess.run([*base, '/opt/keycloak/bin/kcadm.sh', *args, '--config', config], cwd=ROOT, check=True, text=True, **kwargs)
    try:
        subprocess.run([*base, 'bash', '-ec',
            'umask 077; /opt/keycloak/bin/kcadm.sh config credentials --config "$1" --server http://localhost:8080/identity --realm master --user admin --password "$KC_BOOTSTRAP_ADMIN_PASSWORD"',
            'configure-local-email', config], cwd=ROOT, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        yield run
    finally:
        subprocess.run([*base, 'rm', '-f', config], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
