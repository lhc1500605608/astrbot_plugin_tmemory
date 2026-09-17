#!/usr/bin/env bash
# Build a clean, installable AstrBot plugin zip (TMEAAA-378).
#
# Usage:
#   tools/pack_plugin.sh                       # -> dist/astrbot_plugin_tmemory.zip
#   tools/pack_plugin.sh --output /tmp/out.zip
#
# The archive has a single top-level directory (astrbot_plugin_tmemory/) and
# excludes __MACOSX, .DS_Store, ._* resource forks, __pycache__, .env and
# other development artifacts. It is verified against AstrBot's archive root
# resolution rules before the script reports success.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

exec python3 "${script_dir}/plugin_archive.py" pack --repo-root "${repo_root}" "$@"
