# SPDX-License-Identifier: GPL-3.0-only
# Sourced by build.sh, package.sh, install.sh and release.sh: one build of this app at a
# time on this Mac, from any checkout or worktree. They all use
# ~/Library/Caches/I Need A Budget-build, which each build starts by deleting, so a second
# build waits for the first. The lock is held until the script that took it and
# everything it started have exited (killed too: nothing is left locked); the scripts it
# runs, like package.sh's build.sh, see INAB_BUILD_LOCK and don't take it again.

if [[ -z "${INAB_BUILD_LOCK:-}" ]]; then
    export INAB_BUILD_LOCK="$HOME/Library/Caches/I Need A Budget-build.lock"
    exec 9>"$INAB_BUILD_LOCK"
    if ! lockf -s -t 0 9; then
        echo "Waiting for another build of I Need A Budget to finish…" >&2
        lockf -s 9
    fi
fi
