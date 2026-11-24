#!/usr/bin/env bash
# Run this after every pass 2 batch. Prints the sha256 of pass1.json and
# eval_pass1.json plus `git status --short output/` - compare the hashes
# to the previous run by eye; if either changed, or git shows anything
# other than new pass2.json/pass2_failures.json, something wrote to a
# pass 1 file it shouldn't have.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "sha256 of pass1 files:"
sha256sum output/pass1.json output/eval_pass1.json

echo
echo "git status --short output/:"
git status --short output/
