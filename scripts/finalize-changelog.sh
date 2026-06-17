#!/usr/bin/env bash
# Rinomina la sezione "## Unreleased" del CHANGELOG con la versione appena
# calcolata da commitizen. Viene eseguito come pre_bump_hook, quindi
# pyproject.toml è già stato aggiornato alla nuova versione.
set -euo pipefail

CHANGELOG="CHANGELOG.md"

# Legge la nuova versione da pyproject.toml (già aggiornato da commitizen)
NEW_VERSION=$(python3 -c "
import sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('pyproject.toml', 'rb') as f:
    print(tomllib.load(f)['tool']['commitizen']['version'])
")

DATE=$(date +%Y-%m-%d)

if ! grep -q "^## Unreleased" "$CHANGELOG"; then
    echo "Warning: no '## Unreleased' section found in $CHANGELOG — skipping rename" >&2
    exit 0
fi

# Sostituisce solo la prima occorrenza di "## Unreleased"
# compatibile sia con macOS (BSD sed) che Linux (GNU sed)
if [[ "$(uname)" == "Darwin" ]]; then
    sed -i '' "s/^## Unreleased$/## v${NEW_VERSION} (${DATE})/" "$CHANGELOG"
else
    sed -i "s/^## Unreleased$/## v${NEW_VERSION} (${DATE})/" "$CHANGELOG"
fi

git add "$CHANGELOG"
echo "CHANGELOG.md: 'Unreleased' → 'v${NEW_VERSION} (${DATE})'"
