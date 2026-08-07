#!/usr/bin/env bash
set -euo pipefail

source_root="${PANOPTICON_SOURCE_ROOT:-/ctx/panopticon-source/panopticon}"
if [[ ! -d "${source_root}" ]]; then
  exit 0
fi

purelib="${PANOPTICON_PURELIB:-$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')}"
rm --recursive --force "${purelib}/panopticon"
cp --archive "${source_root}" "${purelib}/panopticon"
