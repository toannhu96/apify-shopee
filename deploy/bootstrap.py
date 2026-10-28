"""Create local server secrets once. Does not start services or print secrets."""
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    folder = root / '.secrets'
    env = root / '.env'
    if folder.exists() or env.exists():
        raise SystemExit('Refusing to overwrite existing .secrets or .env; preserve your current configuration.')
    os.umask(0o077)
    folder.mkdir(mode=0o700)
    password = secrets.token_urlsafe(32)
    values = {
        'database_password': password,
        'database_url': f'postgresql://shopee:{password}@db:5432/shopee',
        'api_token': secrets.token_urlsafe(48),
        'encryption_key': Fernet.generate_key().decode(),
        'discord_webhook': '',
    }
    for name, value in values.items():
        with (folder / name).open('x') as output:
            output.write(value + '\n')
    with env.open('x') as output:
        output.write(f'SH_RUN_UID={os.getuid()}\nSH_RUN_GID={os.getgid()}\nSH_LOCAL_PORT=8000\n')
    print('Created protected .secrets/ and .env. Configure Discord and HTTPS before production use.')


if __name__ == '__main__':
    main()
