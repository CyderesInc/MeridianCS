#!/usr/bin/env python3
"""Sign a release package with the Vault Transit release key, writing `<package>.sig` (SSHSIG).

    uv run --locked --group release scripts/tools/sign-release.py <package.skill.zip> [--candidate]
    uv run --locked --group release scripts/tools/sign-release.py --public-key

Log in to Vault first and set VAULT_NAMESPACE: admin/engineering-prod for a release,
admin/engineering-dev with --candidate for a release candidate.

Nothing is written unless two independent checks accept the signature: the package's own verifier,
which is exactly the code and key list an install runs, and OpenSSH's `ssh-keygen -Y verify`, which
trusts none of this repo's code. The dev key signs candidates and the package rightly does not trust
it, so --candidate checks against the signing key instead; installs refuse a candidate.
"""
import argparse
import base64
import importlib.util
import ipaddress
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import zipfile

from hvac import Client as VaultClient
from hvac.exceptions import VaultError

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = "meridiancs-release"
MOUNT = "transit-release"
PACKAGED_VERIFIER = "meridiancs/scripts/meridian.py"


def die(msg, code=1):
    print(msg)
    sys.exit(code)


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def vault():
    url = os.environ.get("VAULT_ADDR", "")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" and not (parts.scheme == "http" and is_loopback(parts.hostname)):
        die("VAULT_ADDR must be https (got %r); plain http is only for a loopback dev server." % url, 2)
    client = VaultClient(url=url, namespace=os.environ.get("VAULT_NAMESPACE") or None)
    if not client.token:
        die("No Vault token: run `vault login` or set VAULT_TOKEN.", 2)
    return client


def is_loopback(host):
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return host == "localhost"


def transit_key(client):
    """(raw Ed25519 public key, version) of the release key's latest version."""
    data = client.secrets.transit.read_key(KEY, mount_point=MOUNT)["data"]
    if data.get("type") != "ed25519":
        die("Transit key %s is %r; installs verify only Ed25519." % (KEY, data.get("type")))
    version = data["latest_version"]
    return base64.b64decode(data["keys"][str(version)]["public_key"]), version


def ssh_public_blob(m, raw):
    return m._ssh_string(b"ssh-ed25519") + m._ssh_string(raw)


def pubkey_line(m, raw, version):
    return "ssh-ed25519 %s %s-vault-v%d" % (base64.b64encode(ssh_public_blob(m, raw)).decode("ascii"),
                                            KEY, version)


def sign(m, client, body):
    """The armored SSHSIG block for `body`, and the signing key's OpenSSH line."""
    raw, version = transit_key(client)
    signed_data = m.sshsig_signed_data(body)
    out = client.secrets.transit.sign_data(KEY, hash_input=base64.b64encode(signed_data).decode("ascii"),
                                           key_version=version, mount_point=MOUNT)
    signature = out["data"]["signature"]
    prefix = "vault:v%d:" % version
    if not signature.startswith(prefix):
        die("Transit signed with a different key version than it reported (%s...)." % signature[:12])
    armored = m.sshsig_armor(ssh_public_blob(m, raw), base64.b64decode(signature[len(prefix):]))
    return armored, pubkey_line(m, raw, version)


def check(package, body, sig, keys):
    """Refuse a signature that the package's own verifier or ssh-keygen rejects. Returns the signer.

    `keys` None means the package's own RELEASE_SIGNING_KEYS: the list every install will judge it by.
    """
    tmp = tempfile.mkdtemp(prefix="sigcheck-")
    try:
        verifier = os.path.join(tmp, "packaged_meridian.py")
        with zipfile.ZipFile(package) as z, open(verifier, "wb") as f:
            f.write(z.read(PACKAGED_VERIFIER))
        packaged = load_module(verifier, "packaged_meridian")
        keys = keys or packaged.RELEASE_SIGNING_KEYS
        try:
            signer = packaged.verify_release_signature(body, sig, keys=keys)
        except ValueError as e:
            die("REJECTED by the package's own verifier: %s\nNothing was written." % e)
        ssh_keygen_verify(package, sig, keys, tmp)
        return signer
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ssh_keygen_verify(package, sig, keys, tmp):
    exe = shutil.which("ssh-keygen")
    if not exe:
        die("ssh-keygen is not on PATH; it is the check that does not trust this repo's code.", 2)
    signers, sig_path = os.path.join(tmp, "allowed_signers"), os.path.join(tmp, "package.sig")
    with open(signers, "w", encoding="ascii") as f:
        f.writelines('%s namespaces="%s" %s\n' % (KEY, KEY, k) for k in keys)
    with open(sig_path, "w", encoding="ascii", newline="\n") as f:
        f.write(sig)
    with open(package, "rb") as f:
        r = subprocess.run([exe, "-Y", "verify", "-f", signers, "-I", KEY, "-n", KEY, "-s", sig_path],
                           stdin=f, capture_output=True, text=True)
    if r.returncode:
        die("REJECTED by ssh-keygen: %s\nNothing was written." % (r.stderr or r.stdout).strip())


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Sign a meridiancs release package through Vault Transit.")
    ap.add_argument("package", nargs="?", help="the .skill.zip to sign")
    ap.add_argument("--candidate", action="store_true",
                    help="release candidate: check against the key that signed, not the package's "
                         "trusted keys. Installs refuse a candidate signature.")
    ap.add_argument("--public-key", action="store_true",
                    help="print the release key's OpenSSH line, for RELEASE_SIGNING_KEYS")
    return ap.parse_args(argv)


def vault_call(fn, *args):
    """fn(*args), with a Vault refusal or an unreachable Vault reported as one line."""
    try:
        return fn(*args)
    except (VaultError, OSError) as e:   # requests' connection and TLS errors are OSErrors
        die("Vault %s: %s" % (os.environ.get("VAULT_ADDR"), e))


def main(argv=None):
    a = parse_args(argv)
    m = load_module(os.path.join(os.path.dirname(HERE), "meridian.py"), "meridian_sshsig")
    if a.public_key:
        print(pubkey_line(m, *vault_call(transit_key, vault())))
        return 0
    if not (a.package and os.path.isfile(a.package)):
        die("Name the package to sign.", 2)
    with open(a.package, "rb") as f:
        body = f.read()
    sig, signing_key = vault_call(sign, m, vault(), body)
    signer = check(a.package, body, sig, [signing_key] if a.candidate else None)
    with open(a.package + ".sig", "w", encoding="ascii", newline="\n") as f:
        f.write(sig)
    print("Signed %s\n  signature: %s.sig\n  key:       %s %s"
          % (a.package, a.package, signer["fingerprint"], signer.get("comment") or ""))
    if a.candidate:
        print("Release candidate: installs trust only RELEASE_SIGNING_KEYS, so they refuse this signature.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
