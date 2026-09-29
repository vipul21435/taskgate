#!/bin/sh
# Offline end-to-end demo: build the sample repository, then run `taskgate check`
# on each pull-request branch the way CI would (branch checked out, diff against
# main). The good pull request must exit 0 and the bad one must exit 1.
#
# Usage: sh examples/demo.sh [WORKDIR]   (default: $TMPDIR/taskgate-demo)
set -eu

work="${1:-${TMPDIR:-/tmp}/taskgate-demo}"
here="$(cd "$(dirname "$0")" && pwd)"
repo="$work/repo"

python "$here/build_sample_repo.py" "$repo"

check_branch() {
    branch="$1"
    expected="$2"
    git -C "$repo" checkout -q "$branch"
    echo
    echo "== taskgate check on $branch (expected exit $expected)"
    set +e
    taskgate check "$repo" --base main --out "$work/out/$branch"
    code=$?
    set -e
    if [ "$code" -ne "$expected" ]; then
        echo "demo: expected exit $expected on $branch, got $code" >&2
        exit 1
    fi
}

check_branch pr/1-integer-determinant 0
check_branch pr/2-word-count 1
git -C "$repo" checkout -q main

echo
echo "demo ok: the good pull request passed and the bad one was blocked"
echo "reports: $work/out"
