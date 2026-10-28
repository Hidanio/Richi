#!/bin/sh
# Official user-local bootstrap. No sudo, shell startup files or workspace access here.
set -eu
command -v python3 >/dev/null 2>&1 || { echo 'Richi requires Python 3.9+ (with venv and sqlite3).' >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo 'Richi requires curl to download its installer.' >&2; exit 1; }
python3 -I -c 'import sys, sqlite3, venv, ensurepip; assert sys.version_info >= (3,9), "Python 3.9+ is required"'
richi_tmp=$(mktemp -d "${TMPDIR:-/tmp}/richi-install.XXXXXXXX")
trap 'rm -rf "$richi_tmp"' EXIT HUP INT TERM
richi_url='https://github.com/Hidanio/Richi/releases/latest/download'
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 120 --max-filesize 52428800 "$richi_url/richi-installer.pyz" -o "$richi_tmp/richi-installer.pyz"
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 60 --max-filesize 1024 "$richi_url/richi-installer.pyz.sha256" -o "$richi_tmp/checksum"
python3 -I -c 'import hashlib,pathlib,sys; p=pathlib.Path(sys.argv[1]); expected=(p/"checksum").read_text().split()[0]; assert len(expected)==64 and hashlib.sha256((p/"richi-installer.pyz").read_bytes()).hexdigest()==expected, "Installer checksum mismatch; retry the download"' "$richi_tmp"
python3 -I "$richi_tmp/richi-installer.pyz" "$@"
