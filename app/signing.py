"""Persistent local code-signing identity. Private material never leaves the Mac."""
import json
import fcntl
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import tempfile

DIRECTORY = Path(os.environ.get('MAMI_SIGNING_DIRECTORY',Path.home() / 'Library/Application Support/Mami Developer/Signing'))
CONFIG = DIRECTORY / 'identity.json'


def trust_command():
    return shlex.join(['/usr/bin/security', 'add-trusted-cert', '-r', 'trustRoot', '-p', 'codeSign',
                       '-k', str(DIRECTORY / 'Mami-signing.keychain-db'), str(DIRECTORY / 'Mami-local-signing.crt')])


def run(args, **kwargs):
    # Never echo commands: security's CLI accepts keychain passwords as arguments.
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


def setup():
    if CONFIG.exists():
        print('Existing signing identity retained:', load()['fingerprint'])
        print('If not yet trusted, run once in Terminal on the Mac and approve the prompt:\n' + trust_command())
        return
    if (Path.home()/'mami-lab/signing/identity.json').exists():
        raise RuntimeError('An existing development signing identity must be migrated; refusing to replace it.')
    if DIRECTORY.exists():
        raise RuntimeError(f'{DIRECTORY} already exists without a completed identity. Inspect it; do not regenerate or overwrite its key.')
    DIRECTORY.mkdir(mode=0o700, parents=True)
    password = secrets.token_urlsafe(48)
    password_file = DIRECTORY / 'keychain-password'
    password_file.write_text(password)
    password_file.chmod(0o600)
    keychain = DIRECTORY / 'Mami-signing.keychain-db'
    previous = shlex.split(run(['/usr/bin/security', 'list-keychains', '-d', 'user'], text=True).stdout)
    try:
        run(['/usr/bin/security', 'create-keychain', '-p', password, str(keychain)])
    finally:
        # Keep the user's normal keychain search order intact.
        run(['/usr/bin/security', 'list-keychains', '-d', 'user', '-s', *previous])
    run(['/usr/bin/security', 'set-keychain-settings', '-lut', '21600', str(keychain)])
    run(['/usr/bin/security', 'unlock-keychain', '-p', password, str(keychain)])
    certificate = DIRECTORY / 'Mami-local-signing.crt'
    # Temporary PEM key is encrypted too; retain an encrypted recovery PKCS#12
    # and the imported keychain, both within this private directory.
    with tempfile.TemporaryDirectory(prefix='identity-', dir=DIRECTORY) as temporary:
        private_key = Path(temporary) / 'private.pem'
        config = Path(temporary) / 'openssl.cnf'
        config.write_text('''[req]
prompt = no
distinguished_name = name
x509_extensions = codesign
[name]
CN = Mami Local Code Signing
O = Mami Local Development
[codesign]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
subjectKeyIdentifier = hash
''')
        run(['/usr/bin/openssl', 'req', '-new', '-x509', '-newkey', 'rsa:3072', '-sha256', '-days', '3650',
             '-config', str(config), '-keyout', str(private_key), '-out', str(certificate),
             '-passout', f'file:{password_file}'])
        archive = DIRECTORY / 'Mami-local-signing.p12'
        run(['/usr/bin/openssl', 'pkcs12', '-export', '-inkey', str(private_key), '-in', str(certificate),
             '-out', str(archive), '-name', 'Mami Local Code Signing',
             '-passin', f'file:{password_file}', '-passout', 'env:MAMI_SIGNING_PASSWORD'],
            env={**os.environ, 'MAMI_SIGNING_PASSWORD': password})
        archive.chmod(0o600)
        run(['/usr/bin/security', 'import', str(archive), '-k', str(keychain), '-P', password,
             '-T', '/usr/bin/codesign'])
    run(['/usr/bin/security', 'set-key-partition-list', '-S', 'apple-tool:,apple:,codesign:', '-s', '-k', password, str(keychain)])
    fingerprint = run(['/usr/bin/openssl', 'x509', '-in', str(certificate), '-noout', '-fingerprint', '-sha1'], text=True).stdout.strip().split('=')[-1].replace(':', '')
    CONFIG.write_text(json.dumps(dict(fingerprint=fingerprint, keychain=str(keychain), passwordFile=str(password_file)), indent=2))
    CONFIG.chmod(0o600)
    print('Created persistent local signing identity:', fingerprint)
    print('Private signing directory:', DIRECTORY)
    print('Run once in Terminal on the Mac and approve the code-signing trust prompt:\n' + trust_command())


def load():
    value = json.loads(CONFIG.read_text())
    if len(value['fingerprint']) != 40 or any(c not in '0123456789ABCDEF' for c in value['fingerprint']):
        raise RuntimeError('Invalid signing certificate fingerprint')
    for key in ('keychain',):
        if not Path(value[key]).is_file():
            raise RuntimeError(f'Missing signing material: {key}. Restore the existing identity; do not replace it.')
    return value


def password_for(identity):
    if 'passwordService' in identity:
        return run(['/usr/bin/security','find-generic-password','-s',identity['passwordService'],
                    '-a',identity['passwordAccount'],'-w'],text=True).stdout.rstrip('\n')
    # Explicit legacy/custom developer identities remain readable during migration.
    return Path(identity['passwordFile']).read_text().strip()


def sign(bundle, nested=()):
    identity = load()
    password = password_for(identity)
    run(['/usr/bin/security', 'unlock-keychain', '-p', password, identity['keychain']])
    requirement = f'designated => identifier "local.mami.prototype" and certificate leaf = H"{identity["fingerprint"]}"'
    # codesign also needs the identity in the user search list, even with an
    # explicit --keychain. Serialize our builds and restore the original list.
    with (DIRECTORY / 'signing.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = shlex.split(run(['/usr/bin/security', 'list-keychains', '-d', 'user'], text=True).stdout)
        try:
            run(['/usr/bin/security', 'list-keychains', '-d', 'user', '-s', *dict.fromkeys([*previous, identity['keychain']])])
            for path in nested:
                run(['/usr/bin/codesign', '--force', '--sign', identity['fingerprint'], '--keychain', identity['keychain'],
                     '--timestamp=none', str(path)])
            run(['/usr/bin/codesign', '--force', '--sign', identity['fingerprint'], '--keychain', identity['keychain'],
                 '--timestamp=none', '--requirements', '=' + requirement, str(bundle)])
        except subprocess.CalledProcessError as error:
            raise RuntimeError('Code signing failed: ' + error.stderr.decode(errors='replace').strip()
                               + '\nIf this is the first build, approve the certificate from Terminal on the Mac:\n'
                               + trust_command()) from None
        finally:
            run(['/usr/bin/security', 'list-keychains', '-d', 'user', '-s', *previous])
    run(['/usr/bin/codesign', '--verify', '--strict', '--verbose=2', str(bundle)])
    details = run(['/usr/bin/codesign', '-d', '-r-', '--verbose=4', str(bundle)], text=True)
    (bundle.parent / 'signing-verification.txt').write_text(details.stdout + details.stderr)


if __name__ == '__main__':
    try:
        setup()
    except subprocess.CalledProcessError as error:
        # stdout from security can include key attributes; report only stderr.
        raise SystemExit(f'Signing setup failed ({Path(error.cmd[0]).name}, exit {error.returncode}): {error.stderr.decode(errors="replace")}') from None
