#!/bin/bash
# Enhanced update script with minimal output

# Drain the persistent Assistant before replacing any helper it attests. The root API
# owns a separate gate for the entire update, so auto-connect cannot restart it midway.
if [ "${DDISTRO_ASSISTANT_MAINTENANCE:-}" != 1 ] && [ -x /usr/local/bin/ddistro_api ] \
    && [ -d /var/lib/dwemerdistro/assistant/agent ]; then
    exec /usr/bin/python3 /home/dwemer/dwemerdistro/bin/ddistro_api maintenance /bin/bash "$0" "$@"
fi

# Simple color palette
GREEN='\033[0;32m'
RED='\033[1;31m'
BOLD='\033[1m'
NC='\033[0m' # No Color

CURRENT_STEP="starting the core update"
CORE_ERROR_REPORTED=0

# Print a compact section header; it also names the step reported if a command fails silently.
print_header() {
    CURRENT_STEP="$1"
    printf "${BOLD}== %s ==${NC}\n" "$1"
}

# Print one progress step.
print_step() {
    CURRENT_STEP="$1"
    printf ">> %s...\n" "$1"
}

print_error() {
    CORE_ERROR_REPORTED=1
    printf "${RED}[ERROR] %s${NC}\n" "$1"
}

# Any non-zero exit that did not already print an error names the step, so no failure is silent.
report_unexplained_failure() {
    local status="$1"
    if [ "$status" -ne 0 ] && [ "$CORE_ERROR_REPORTED" -ne 1 ]; then
        printf "${RED}[ERROR] Core update failed at step: %s (exit %s).${NC}\n" "$CURRENT_STEP" "$status"
    fi
}

# Main title
printf "${BOLD}Core system update${NC}\n"

print_header "CHECKING SYSTEM"

print_step "Checking source directories"
if [ ! -d "/home/dwemer/dwemerdistro/bin" ]; then
    print_error "Source directory /home/dwemer/dwemerdistro/bin not found!"
    exit 1
fi
if [ ! -d "/home/dwemer/dwemerdistro/etc" ]; then
    print_error "Source directory /home/dwemer/dwemerdistro/etc not found!"
    exit 1
fi

print_step "Checking Git credential helper configuration"
DWEMER_GIT_CONFIG="/home/dwemer/.gitconfig"
if [ -f "$DWEMER_GIT_CONFIG" ] && ! command -v git-credential-manager >/dev/null 2>&1; then
    if git config --file "$DWEMER_GIT_CONFIG" --get-all credential.helper 2>/dev/null | grep -Fxq "manager"; then
        if ! git config --file "$DWEMER_GIT_CONFIG" --unset-all credential.helper '^manager$'; then
            print_error "Could not remove the unavailable Git credential helper."
            exit 1
        fi
        chown dwemer:dwemer "$DWEMER_GIT_CONFIG" || exit 1
        printf "${GREEN}[SUCCESS] Removed unavailable Git credential helper.${NC}\n"
    fi
fi

# An abrupt stop during PostgreSQL 17 maintenance can leave both clusters stopped, and this update
# starts PostgreSQL and needs SQL below. Recover first: the helper takes the maintenance lock
# exclusively, so it runs before the shared lock, from the source because the installed copy may
# predate this action. It exits at once unless the journal records an unfinished phase.
print_step "Checking for interrupted PostgreSQL maintenance"
recover_status=0
/usr/bin/python3 /home/dwemer/dwemerdistro/bin/ddistro_pg17_maintenance recover || recover_status=$?
if [ "$recover_status" -eq 75 ]; then
    # Exit 75 means a lock was busy, so recovery did not start; nothing was changed or found broken.
    print_error "PostgreSQL or DwemerDistro maintenance is still running, so this update did not start. Retry the update after it finishes."
    exit 75
elif [ "$recover_status" -ne 0 ]; then
    print_error "Interrupted PostgreSQL maintenance could not be recovered automatically."
    exit 1
fi

MAINTENANCE_LOCK="/run/lock/dwemerdistro-maintenance.lock"

# Hold the common maintenance lock shared on fd 7. ddistro changes apply/rollback take it
# exclusively, so they never interleave with maintenance; shared holders (including nested
# helpers) still serialize through their own locks. Root creates the fixed root-owned file
# once; it is never replaced. Release with flock -u, which also covers services that
# inherited fd 7.
acquire_maintenance_lock() {
    local info=""
    if [ "$(id -u)" -eq 0 ] && [ ! -e "$MAINTENANCE_LOCK" ] && [ ! -L "$MAINTENANCE_LOCK" ]; then
        (umask 022; set -C; : > "$MAINTENANCE_LOCK") 2>/dev/null || true
    fi
    if [ -f "$MAINTENANCE_LOCK" ] && [ ! -L "$MAINTENANCE_LOCK" ] && exec 7<"$MAINTENANCE_LOCK"; then
        info="$(stat -L -c '%u %h %a' /proc/self/fd/7 2>/dev/null)"
    fi
    case "$info" in
        "0 1 644") ;;
        "0 1 "*) [ "$(id -u)" -eq 0 ] && chmod 0644 /proc/self/fd/7 || info="" ;;
        *) info="" ;;
    esac
    if [ -z "$info" ]; then
        exec 7<&-
        print_error "$MAINTENANCE_LOCK is not a root-owned plain file; it was not replaced."; exit 1
    fi
    if ! flock -s -w 30 7; then
        exec 7<&-
        print_error "Another DwemerDistro maintenance or ddistro changes operation is running."; exit 75
    fi
}

acquire_maintenance_lock
trap 'core_status=$?; flock -u 7 2>/dev/null || true; report_unexplained_failure "$core_status"' EXIT

print_header "UPDATING SYSTEM FILES"

print_step "Copying scripts to /usr/local/bin"
cp /home/dwemer/dwemerdistro/bin/* /usr/local/bin/ 2>/dev/null
if [ $? -eq 0 ]; then
    # Set executable permissions for all scripts
    chmod +x /usr/local/bin/* 2>/dev/null
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_pg17_maintenance /usr/local/bin/ddistro_pg17_maintenance || exit 1
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_stage_pg17 /usr/local/bin/ddistro_stage_pg17 || exit 1
    # LM Studio runs as dwemer, so do not inherit root-only modes or the updater's umask.
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_lmstudio /usr/local/bin/ddistro_lmstudio || exit 1
    # The launcher reads custom mod status as dwemer, so the manager must stay world-readable.
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_custom_mod /usr/local/bin/ddistro_custom_mod || exit 1
    # Startup runs the PocketTTS connector helper as dwemer, so it must not stay root-only.
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/sync_pockettts_connectors /usr/local/bin/sync_pockettts_connectors || exit 1
    # Every user runs the ddistro CLI for status and logs; only root may change it.
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro /usr/local/bin/ddistro || exit 1
    # Root-only file changes helper; ddistro checks it is root-owned before running it.
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_changes /usr/local/bin/ddistro_changes || exit 1
    # Read-only access policy library; ddistro loads it only when it is root-owned and root-writable only.
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_access /usr/local/bin/ddistro_access || exit 1
    # Sandboxed command helper for read-only file snapshots; ddistro runs it only when it is root-owned.
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_exec /usr/local/bin/ddistro_exec || exit 1
    # Live command supervisor for the Assistant's approved runs; the broker starts it only while the probe attests it.
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_run /usr/local/bin/ddistro_run || exit 1
    # Assistant runtime helpers. Only the scripts are installed here: the pinned Codex runtime,
    # bubblewrap, and the ddagent account come from an explicit install_assistant_runtime run.
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_agent /usr/local/bin/ddistro_agent || exit 1
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_api /usr/local/bin/ddistro_api || exit 1
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/install_shared_keys /usr/local/bin/install_shared_keys || exit 1
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_keys /usr/local/bin/ddistro_keys || exit 1
    # Dashboard API Keys rail: masked status, existing-key edits, new catalog keys and saved-key tests, through one exact four-verb root rule.
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_dashboard_keys /usr/local/bin/ddistro_dashboard_keys || exit 1
    # Global API Keys consumer socket (/mnt/wsl); ensure restarts a stale server only when a consumer is enrolled.
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_keys_api /usr/local/bin/ddistro_keys_api || exit 1
    if /usr/local/bin/ddistro_keys_api clients; then
        /usr/local/bin/ddistro_keys_api ensure >/dev/null || echo "Global API Keys consumer socket did not restart; run ddistro_keys_api ensure."
    fi
    dashboard_keys_rule=$(mktemp) || exit 1
    printf '%s\n' 'www-data ALL=(root) NOPASSWD: /usr/local/bin/ddistro_dashboard_keys status, /usr/local/bin/ddistro_dashboard_keys edit, /usr/local/bin/ddistro_dashboard_keys create, /usr/local/bin/ddistro_dashboard_keys test' > "$dashboard_keys_rule"
    CURRENT_STEP="validating and installing the dashboard keys sudoers rule"
    if ! visudo -cqf "$dashboard_keys_rule" || ! install -o root -g root -m 440 "$dashboard_keys_rule" /etc/sudoers.d/dwemerdistro-dashboard-keys; then
        rm -f "$dashboard_keys_rule"; exit 1
    fi
    rm -f "$dashboard_keys_rule"
    install -d -o root -g root -m 755 /usr/local/lib/dwemerdistro || exit 1
    install -o root -g root -m 644 /home/dwemer/dwemerdistro/bin/ddistro_keys.php /usr/local/lib/dwemerdistro/keys.php || exit 1
    install -o root -g root -m 644 /home/dwemer/dwemerdistro/bin/ddistro_native_keys.php /usr/local/lib/dwemerdistro/native_keys.php || exit 1
    install -o root -g root -m 644 /home/dwemer/dwemerdistro/bin/ddistro_shared_keys.php /usr/local/lib/dwemerdistro/shared_keys.php || exit 1
    install -o root -g root -m 644 /home/dwemer/dwemerdistro/bin/ddistro_legacy_keys.php /usr/local/lib/dwemerdistro/legacy_keys.php || exit 1
    install -o root -g root -m 644 /home/dwemer/dwemerdistro/bin/ddistro_legacy_keys.sql /usr/local/lib/dwemerdistro/legacy_keys.sql || exit 1
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/install_assistant_runtime /usr/local/bin/install_assistant_runtime || exit 1
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_mod_release.py /usr/local/bin/ddistro_mod_release.py || exit 1
    printf "${GREEN}[SUCCESS] Scripts successfully copied to /usr/local/bin/${NC}\n"
else
    print_error "Error copying scripts to /usr/local/bin/"
    exit 1
fi

# Repair old image keyrings before any subsequent component installation uses APT.
CURRENT_STEP="repairing the GitHub CLI keyring"
/usr/local/bin/repair_github_cli_keyring || exit 1

# Patch the shared PHP runtime before any server, migration, or configuration step runs PHP.
print_step "Verifying the PHP security update"
if ! /usr/local/bin/ensure_php_security; then
    print_error "The PHP security update is incomplete. Server files were not updated."
    exit 1
fi

print_step "Checking installed server repository origins"
/usr/local/bin/ddistro_server migrate-remotes all || exit 1

print_step "Ensuring UTF-8 is the PostgreSQL default"
update-locale LANG=C.UTF-8 LC_ALL=C.UTF-8
CURRENT_STEP="starting PostgreSQL"
service postgresql start >/dev/null 7<&- || exit 1
CURRENT_STEP="ensuring UTF-8 is the PostgreSQL default"
/usr/local/bin/ensure_postgres_utf8 || exit 1

# Replace a shipped configuration file only while it still matches what an update installed.
# A file the user changed is kept; a missing file gets the shipped default. Updates from before
# the recorded baseline existed pass the SHA-256 of each earlier shipped copy, which counts as unchanged.
SHIPPED_CONFIG_STATE=/var/lib/dwemerdistro/shipped-config
install_shipped_config() {
    local source="$1" target="$2" mode="$3"
    local baseline="$SHIPPED_CONFIG_STATE/$(basename "$target").sha256"
    local shipped current recorded="" previous

    shipped="$(sha256sum < "$source" | cut -d ' ' -f 1)" || return 1
    if [ -L "$target" ] || { [ -e "$target" ] && [ ! -f "$target" ]; }; then
        print_error "$target is not a plain file. It was not changed."
        return 1
    fi
    if [ -f "$target" ]; then
        current="$(sha256sum < "$target" | cut -d ' ' -f 1)" || return 1
        if [ -f "$baseline" ]; then
            recorded="$(head -n 1 "$baseline")"
        else
            for previous in "${@:4}"; do
                [ "$current" != "$previous" ] || recorded="$previous"
            done
        fi
        if [ "$current" != "$shipped" ] && [ "$current" != "$recorded" ]; then
            printf ">> Keeping customized %s\n" "$target"
            return 0
        fi
    fi
    install -d -o root -g root -m 755 "$SHIPPED_CONFIG_STATE" \
        && install -o root -g root -m "$mode" "$source" "$target" \
        && printf '%s\n' "$shipped" > "$baseline"
}

print_step "Copying configuration files to /etc"
find /home/dwemer/dwemerdistro/etc/ -type f ! -name "php.ini" ! -name "dwemerdistro_services.conf" \
    ! -name "dwemerdistro-lmstudio.json" ! -name "cuda_config" -exec cp {} /etc/ \; 2>/dev/null
if [ $? -eq 0 ]; then
    # The LM Studio helper runs as dwemer and must read the public model catalog. The hash is the
    # catalog shipped before the recorded baseline (v1.0.7 and v1.0.8).
    install_shipped_config /home/dwemer/dwemerdistro/etc/dwemerdistro-lmstudio.json /etc/dwemerdistro-lmstudio.json 644 \
        77e1415be85be014231aa5eea3d29bfd08cbfa949b7d23fd55bfccff8b098dd9 || exit 1
    install_shipped_config /home/dwemer/dwemerdistro/etc/cuda_config /etc/cuda_config 644 || exit 1
    install -d -o root -g root -m 755 /etc/dwemer || exit 1
    install -o root -g root -m 644 /home/dwemer/dwemerdistro/etc/shared-key-runtime.json /etc/dwemer/shared-key-runtime.json || exit 1
    # The validated shared component catalog is shipped with the Assistant.
    install -o root -g root -m 644 /home/dwemer/dwemerdistro/etc/dwemerdistro-components.json /etc/dwemerdistro-components.json || exit 1
    # Keep the existing service ports, including CUSTOM_MODS_PORT; only a missing file gets the defaults.
    # start_env sources it as root, so an existing file must be a plain root-owned file only root can change.
    SERVICES_CONF=/etc/dwemerdistro_services.conf
    if [ ! -e "$SERVICES_CONF" ] && [ ! -L "$SERVICES_CONF" ]; then
        install -o root -g root -m 644 /home/dwemer/dwemerdistro/etc/dwemerdistro_services.conf "$SERVICES_CONF" || exit 1
    elif [ -L "$SERVICES_CONF" ] || [ ! -f "$SERVICES_CONF" ] || [ "$(stat -c '%u' "$SERVICES_CONF")" != 0 ] \
        || [ $(( 0$(stat -c '%a' "$SERVICES_CONF") & 022 )) -ne 0 ]; then
        print_error "$SERVICES_CONF must be a plain root-owned file that only root can change. It was not changed."
        exit 1
    fi
    if ! runuser -u dwemer -- python3 -c 'import json, sys; sys.exit(not isinstance(json.load(open(sys.argv[1]))["models"], list))' /etc/dwemerdistro-lmstudio.json; then
        print_error "LM Studio model catalog is not readable by dwemer"
        exit 1
    fi
    if ! runuser -u dwemer -- python3 -c 'import json, sys; sys.exit(not isinstance(json.load(open(sys.argv[1]))["components"], list))' /etc/dwemerdistro-components.json; then
        print_error "Component catalog is not readable by dwemer"
        exit 1
    fi
    printf "${GREEN}[SUCCESS] Configuration files successfully copied to /etc/${NC}\n"
else
    print_error "Error copying configuration files to /etc/"
    exit 1
fi

print_header "UPDATING PHP CONFIGURATION"

# Only a freshly prepared image has the marker that enables this initialization.
CURRENT_STEP="initializing a fresh distro"
/usr/local/bin/initialize_fresh_distro || exit 1

# Existing PHP settings belong to the user; only a missing Apache php.ini gets the distro default.
print_step "Checking PHP Apache configuration"
PHP_APACHE_INI=/etc/php/8.2/apache2/php.ini
if [ -e "$PHP_APACHE_INI" ] || [ -L "$PHP_APACHE_INI" ]; then
    : # The user's existing PHP configuration is kept; nothing to report.
elif install -o root -g root -m 644 /home/dwemer/dwemerdistro/etc/php.ini "$PHP_APACHE_INI"; then
    printf "${GREEN}[SUCCESS] Default PHP configuration installed${NC}\n"
else
    print_error "Error installing the default PHP configuration"
    exit 1
fi

# Existing and fresh users receive the same hash-verified runtime, without account sign-in.
# An interrupted download can be retried; no user data or provider credentials are replaced.
CURRENT_STEP="installing the Assistant runtime"
/usr/local/bin/install_assistant_runtime --install-prerequisites 7<&- || exit 1
# Install the immutable credential library without provisioning operator keys into redistributable images.
CURRENT_STEP="installing the shared keys runtime"
/usr/local/bin/install_shared_keys --install-runtime 7<&- || exit 1
# Enable installed shared-key adapters only after their central deletion guard is ready; never import user keys here.
if [ "${DDISTRO_KEYS_IMAGE_BUILD:-}" != 1 ] && ! /usr/local/bin/ddistro_keys prepare-legacy >/dev/null; then
    printf "${YELLOW}[WARNING] Shared API Keys setup is not ready. Existing keys are preserved; update CHIM 4 and the server adapters, then retry Update Distro.${NC}\n"
fi
# Shared key references resolve at request time; do not synchronize legacy copies.
# Do not report a successful Assistant upgrade with a skipped or failed isolation check.
if ! /usr/local/bin/ddistro --json agent probe 7<&- | /usr/bin/python3 -c '
import json, sys
result = json.load(sys.stdin)
sys.exit(0 if result.get("ok") is True and result.get("data", {}).get("passed") is True else 1)
'; then
    print_error "Assistant isolation did not pass. Check connectivity and retry Update Distro."
    exit 1
fi

printf "${GREEN}[SUCCESS] Core system updated.${NC}\n"

#DO NOT REMOVE Begin CHIM Server Update as this triggers the update_gws step in the CHIM launcher.
echo "Begin CHIM Server Update"
