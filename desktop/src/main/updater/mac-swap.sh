#!/bin/sh
# Replace COGITO.app with a verified, already-extracted new bundle, after the app has quit.
#
# Spawned detached by mac.js, which has already checked the download against the signed
# manifest and extracted it next to the bundle (same volume, so every mv is a rename).
# The old bundle is kept until the new one proves it starts: its executable runs with
# --cogito-update-selftest=<marker> and must exit 0 after writing the expected version
# into the marker. Only then is the old bundle deleted. Any failure puts the old bundle
# back. In restart mode the app (new or old) is opened at the end. In quit mode it is not.
#
# The two renames are not one atomic operation (Node has no RENAME_SWAP). The window
# between them is two system calls wide.
#
# Writes one word to <workdir>/result: ok | rolled-back | aborted | failed | broken,
# and a log to <workdir>/swap.log. Plain POSIX sh, no bash-only syntax.
#
# usage: mac-swap.sh PID TARGET STAGED BACKUP EXE_REL WORKDIR MODE LAUNCHER VERSION TIMEOUT_S

set -u

pid=$1
target=$2
staged=$3
backup=$4
exe_rel=$5
workdir=$6
mode=$7
launcher=$8
version=$9
shift 9
timeout=${1:-60}
ticks=$((timeout * 10))
marker="$workdir/launched"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" >> "$workdir/swap.log"
}

finish() {
    printf '%s\n' "$1" > "$workdir/result"
    log "result: $1"
    exit "$2"
}

# `open` returns once LaunchServices has the request (it does not wait for the app), so it
# runs in the foreground. The result is written only after the app was asked to start.
open_app() {
    if [ "$mode" = restart ]; then
        "$launcher" "$target" >/dev/null 2>&1 || log "could not open $target"
    fi
}

log "start: pid=$pid target=$target staged=$staged mode=$mode version=$version"

# 1. Wait for the old process to exit. If it never does, change nothing. The staged
#    bundle stays where it is for the next attempt.
waited=0
while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$ticks" ]; then
        finish aborted 1
    fi
    sleep 0.1
    waited=$((waited + 1))
done

# 2. Nothing is moved unless both bundles are where they should be.
if [ ! -d "$target" ] || [ ! -x "$staged/$exe_rel" ]; then
    finish failed 1
fi

rm -rf "$backup"
if ! mv "$target" "$backup"; then
    finish failed 1
fi
if ! mv "$staged" "$target"; then
    if mv "$backup" "$target"; then
        open_app
        finish rolled-back 1
    fi
    log "could not restore $backup to $target"
    finish broken 2
fi
log "swapped"

# 3. Self-test, bounded by the same timeout.
rm -f "$marker"
"$target/$exe_rel" "--cogito-update-selftest=$marker" >/dev/null 2>&1 &
child=$!
waited=0
while kill -0 "$child" 2>/dev/null; do
    if [ "$waited" -ge "$ticks" ]; then
        log "self-test did not finish in ${timeout}s"
        kill -9 "$child" 2>/dev/null
        break
    fi
    sleep 0.1
    waited=$((waited + 1))
done
wait "$child"
status=$?

reported=""
if [ -f "$marker" ]; then
    reported=$(cat "$marker")
fi

if [ "$status" -eq 0 ] && [ "$reported" = "$version" ]; then
    rm -rf "$backup"
    open_app
    finish ok 0
fi

# 4. Roll back.
log "self-test failed: status=$status reported='$reported'"
rejected="$workdir/rejected.app"
rm -rf "$rejected"
mv "$target" "$rejected" 2>/dev/null || rm -rf "$target"
if mv "$backup" "$target"; then
    open_app
    finish rolled-back 1
fi
log "could not restore $backup to $target"
finish broken 2
