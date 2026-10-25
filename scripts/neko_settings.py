#!/usr/bin/env python3
"""Signed remote settings for the NekoVPN app (client plan E14).

    python3 scripts/neko_settings.py keygen   once: the private key, offline
    python3 scripts/neko_settings.py sign     bot/neko/settings.source.json
                                               -> bot/neko/settings.json
    python3 scripts/neko_settings.py verify   the signed file, with PUBLIC_KEY
    python3 scripts/neko_settings.py show     what the app gets

Runs on the dev machine, never on the server: the private key lives in
~/.config/nekovpn-signing (next to the APK signing key) and nowhere else, so
neither a mirror, a CDN cache nor the server itself can forge settings. The
bot only serves the signed file as is (GET /neko/settings); the app checks
the signature with the public key it was built with.

The signed file: {"kid", "doc": base64(doc bytes), "sig": base64(Ed25519
over exactly those bytes)}, so no JSON canonicalisation is involved. doc =
the source settings plus "version" (one more than the signed file had; the
app takes only a bigger one) and "signed_at". Publishing = sign, commit,
deploy (scripts/deploy_to_entry.sh). Source fields: client lib/neko/
remote_settings.dart and AGENTS.md section 40.
"""

import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'bot' / 'neko' / 'settings.source.json'
SIGNED = ROOT / 'bot' / 'neko' / 'settings.json'
KEY_PATH = Path.home() / '.config' / 'nekovpn-signing' / 'neko-settings-ed25519.pem'
KID = 'neko-settings-1'
# The raw Ed25519 public key the app is built with (client
# lib/neko/remote_settings.dart, nekoSettingsPublicKey). Rotating it means
# a client release first: apps only trust the key they carry.
PUBLIC_KEY = 'ID6b87cc4SRPMv/CW3y0cvHh3TRvyK7ytx6/6Db6R7I='

_VERSION = re.compile(r'^\d+\.\d+\.\d+-neko\.\d+$')
FIELDS = {'schema', 'endpoints', 'min_version', 'message', 'flags'}


def validate(settings):
    """Raise ValueError when the app could not use these settings."""
    if not isinstance(settings, dict):
        raise ValueError('settings: an object')
    unknown = set(settings) - FIELDS
    if unknown:
        raise ValueError(f'unknown fields: {sorted(unknown)}')
    if settings.get('schema') != 1:
        raise ValueError('schema: 1')
    endpoints = settings.get('endpoints')
    if (not isinstance(endpoints, list) or not endpoints or len(endpoints) > 10
            or not all(isinstance(e, str) and re.match(r'^https://[^/\s]+$', e)
                       for e in endpoints)):
        raise ValueError('endpoints: 1-10 https origins, no path')
    min_version = settings.get('min_version')
    if min_version is not None and not (
            isinstance(min_version, str) and _VERSION.match(min_version)):
        raise ValueError('min_version: null or "x.y.z-neko.n"')
    message = settings.get('message')
    if message is not None:
        if not isinstance(message, dict) or set(message) - {'id', 'ru', 'en', 'url'}:
            raise ValueError('message: {id, ru, en?, url?}')
        if not (isinstance(message.get('id'), str) and message['id'].strip()):
            raise ValueError('message.id: a non-empty string, new for every message')
        if not (isinstance(message.get('ru'), str) and message['ru'].strip()):
            raise ValueError('message.ru: the text')
        if 'en' in message and not isinstance(message['en'], str):
            raise ValueError('message.en: a string')
        if 'url' in message and not (
                isinstance(message['url'], str) and message['url'].startswith('https://')):
            raise ValueError('message.url: an https link')
    flags = settings.get('flags')
    if not isinstance(flags, dict) or not all(
            isinstance(k, str) and isinstance(v, (bool, int, float, str))
            for k, v in flags.items()):
        raise ValueError('flags: an object of bool / number / string values')


def decode(signed):
    """(doc dict, doc bytes, signature bytes) of a signed file's object."""
    doc_bytes = base64.b64decode(signed['doc'], validate=True)
    sig = base64.b64decode(signed['sig'], validate=True)
    return json.loads(doc_bytes), doc_bytes, sig


def verify(signed, public_key=None):
    """The doc when the signature holds, else raises."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    doc, doc_bytes, sig = decode(signed)
    key = base64.b64decode(public_key or PUBLIC_KEY, validate=True)
    Ed25519PublicKey.from_public_bytes(key).verify(sig, doc_bytes)
    return doc


def current_version(path=SIGNED):
    try:
        doc, _, _ = decode(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError, KeyError):
        return 0
    version = doc.get('version')
    return version if isinstance(version, int) else 0


def build_doc(settings, version, now=None):
    validate(settings)
    stamp = (now or datetime.now(timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {**settings, 'version': version, 'signed_at': stamp}


def sign_doc(doc, private_key):
    """The signed file's object for this doc."""
    doc_bytes = json.dumps(doc, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':')).encode('utf-8')
    return {
        'kid': KID,
        'doc': base64.b64encode(doc_bytes).decode('ascii'),
        'sig': base64.b64encode(private_key.sign(doc_bytes)).decode('ascii'),
    }


def _load_private_key(path=KEY_PATH):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    return load_pem_private_key(path.read_bytes(), password=None)


def _public_b64(private_key):
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode('ascii')


def main(argv=None, out=sys.stdout):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('command', choices=['keygen', 'sign', 'verify', 'show'])
    args = ap.parse_args(argv)

    if args.command == 'keygen':
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, NoEncryption, PrivateFormat,
        )
        if KEY_PATH.exists():
            print(f'ERROR: {KEY_PATH} exists; a new key strands every app', file=out)
            return 1
        key = Ed25519PrivateKey.generate()
        KEY_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        print(f'public key: {_public_b64(key)}', file=out)
        return 0

    if args.command == 'sign':
        key = _load_private_key()
        if _public_b64(key) != PUBLIC_KEY:
            print('ERROR: the private key does not match PUBLIC_KEY', file=out)
            return 1
        settings = json.loads(SOURCE.read_text(encoding='utf-8'))
        try:
            doc = build_doc(settings, current_version() + 1)
        except ValueError as e:
            print(f'ERROR: {e}', file=out)
            return 1
        SIGNED.write_text(json.dumps(sign_doc(doc, key), indent=1) + '\n', encoding='utf-8')
        print(f'signed version {doc["version"]} -> {SIGNED.relative_to(ROOT)}', file=out)
        return 0

    signed = json.loads(SIGNED.read_text(encoding='utf-8'))
    doc = verify(signed) if args.command == 'verify' else decode(signed)[0]
    print(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True), file=out)
    if args.command == 'verify':
        print('signature OK', file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
