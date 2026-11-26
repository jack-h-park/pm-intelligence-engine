#!/bin/sh
# Resolve the Engine's current launchd registration for the Makefile.
set -eu

label=com.jackpark.pm-engine
system_target=system/$label
system_plist=/Library/LaunchDaemons/$label.plist
gui_target=gui/$(id -u)/$label

status=0
details=$(launchctl print "$system_target" 2>/dev/null) || status=$?
if [ "$status" -ne 0 ] && [ "$status" -ne 113 ]; then
    echo "Could not inspect $system_target (launchctl exit $status)." >&2
    exit 1
fi

case "$1" in
    require-gui)
        if [ "$status" -eq 0 ] || [ -e "$system_plist" ] || [ -L "$system_plist" ]; then
            echo "The $system_target service is configured. Ask an administrator to manage it in the system domain." >&2
            exit 1
        fi
        ;;
    status)
        if [ "$status" -eq 0 ]; then
            printf '%s\n' "$details" | awk 'NR == 1 || /^[[:space:]]*(path|state|username|pid|runs|last exit code) = /'
        elif [ -e "$system_plist" ] || [ -L "$system_plist" ]; then
            echo "$system_target is installed but not loaded."
        elif details=$(launchctl print "$gui_target" 2>/dev/null); then
            printf '%s\n' "$details" | awk 'NR == 1 || /^[[:space:]]*(path|state|username|pid|runs|last exit code) = /'
        else
            echo "$gui_target is not loaded (run 'make install-service')."
        fi
        ;;
    *)
        echo "Usage: $0 require-gui|status" >&2
        exit 2
        ;;
esac
