#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec "${HILLEYE_PYTHON:-python3}" -m core.maintenance backup "$@"
