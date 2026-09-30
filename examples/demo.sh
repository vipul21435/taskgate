#!/bin/sh
# Offline end-to-end demo: build the sample repository, then run `taskgate check`
# on each pull-request branch the way CI would (branch checked out, diff against
# main). The good pull request must exit 0 and each of the three flawed ones 1.
# Then the good branch is checked again: its task is unchanged, so its results
# must come from the result cache (.taskgate/cache in the repository). Finally
# github_demo.py checks the fourth pull request with its file list taken from an
# in-process fake of the GitHub API and publishes the report to it.
#
# Usage: sh examples/demo.sh [WORKDIR]   (default: $TMPDIR/taskgate-demo)
#
# Solutions and graders run on the local runner unless TASKGATE_RUNNER says
# otherwise (`make demo-docker` sets it to docker).
set -eu
export TASKGATE_RUNNER="${TASKGATE_RUNNER:-local}"

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
check_branch pr/3-gcd-pairs 1
check_branch pr/4-log-levels 1

git -C "$repo" checkout -q pr/1-integer-determinant
echo
echo "== taskgate check on pr/1-integer-determinant again (unchanged, expected from the cache)"
again="$(taskgate check "$repo" --base main)"
echo "$again"
case "$again" in
*"PASS  (cached)"*) ;;
*)
    echo "demo: expected the unchanged task's results to come from the cache" >&2
    exit 1
    ;;
esac
taskgate cache stats "$repo"
git -C "$repo" checkout -q main

echo
echo "== GitHub reporting against the in-process fake API (pull request 4)"
python "$here/github_demo.py" "$repo" "$work/out"

echo
echo "demo ok: the good pull request passed, all three flawed ones were blocked,"
echo "the unchanged task was not checked a second time, and the fourth pull request's"
echo "report was posted to the fake GitHub API as one comment and one check run"
echo "reports: $work/out"
