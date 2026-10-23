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

# Print a header
print_header() {
    printf "\n${BOLD}-------------------------------------------------------${NC}\n"
    printf "${BOLD} %s ${NC}\n" "$1"
    printf "${BOLD}-------------------------------------------------------${NC}\n"
}

# Main title
printf "\n${BOLD}=======================================================${NC}\n"
printf "${BOLD}             CORE SYSTEM UPDATER                  ${NC}\n"
printf "${BOLD}=======================================================${NC}\n\n"

print_header "CHECKING SYSTEM"

printf ">> Checking source directories...\n"
if [ ! -d "/home/dwemer/dwemerdistro/bin" ]; then
    printf "${RED}[ERROR] Source directory /home/dwemer/dwemerdistro/bin not found!${NC}\n"
    exit 1
fi
if [ ! -d "/home/dwemer/dwemerdistro/etc" ]; then
    printf "${RED}[ERROR] Source directory /home/dwemer/dwemerdistro/etc not found!${NC}\n"
    exit 1
fi

printf ">> Checking Git credential helper configuration...\n"
DWEMER_GIT_CONFIG="/home/dwemer/.gitconfig"
if [ -f "$DWEMER_GIT_CONFIG" ] && ! command -v git-credential-manager >/dev/null 2>&1; then
    if git config --file "$DWEMER_GIT_CONFIG" --get-all credential.helper 2>/dev/null | grep -Fxq "manager"; then
        if ! git config --file "$DWEMER_GIT_CONFIG" --unset-all credential.helper '^manager$'; then
            printf "${RED}[ERROR] Could not remove the unavailable Git credential helper.${NC}\n"
            exit 1
        fi
        chown dwemer:dwemer "$DWEMER_GIT_CONFIG" || exit 1
        printf "${GREEN}[SUCCESS] Removed unavailable Git credential helper.${NC}\n"
    fi
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
        printf "${RED}[ERROR] $MAINTENANCE_LOCK is not a root-owned plain file; it was not replaced.${NC}\n"; exit 1
    fi
    if ! flock -s -w 30 7; then
        exec 7<&-
        printf "${RED}[ERROR] Another DwemerDistro maintenance or ddistro changes operation is running.${NC}\n"; exit 75
    fi
}

acquire_maintenance_lock
trap 'flock -u 7 2>/dev/null || true' EXIT

print_header "UPDATING SYSTEM FILES"

printf ">> Copying scripts to /usr/local/bin...\n"
cp /home/dwemer/dwemerdistro/bin/* /usr/local/bin/ 2>/dev/null
if [ $? -eq 0 ]; then
    # Set executable permissions for all scripts
    chmod +x /usr/local/bin/* 2>/dev/null
    # LM Studio runs as dwemer, so do not inherit root-only modes or the updater's umask.
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_lmstudio /usr/local/bin/ddistro_lmstudio || exit 1
    # The launcher reads custom mod status as dwemer, so the manager must stay world-readable.
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/ddistro_custom_mod /usr/local/bin/ddistro_custom_mod || exit 1
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
    install -o root -g root -m 755 /home/dwemer/dwemerdistro/bin/install_assistant_runtime /usr/local/bin/install_assistant_runtime || exit 1
    printf "${GREEN}[SUCCESS] Scripts successfully copied to /usr/local/bin/${NC}\n"
else
    printf "${RED}[ERROR] Error copying scripts to /usr/local/bin/${NC}\n"
    exit 1
fi

# Repair old image keyrings before any subsequent component installation uses APT.
/usr/local/bin/repair_github_cli_keyring || exit 1

# Patch the shared PHP runtime before any server, migration, or configuration step runs PHP.
printf ">> Verifying the PHP security update...\n"
if ! /usr/local/bin/ensure_php_security; then
    printf "${RED}[ERROR] The PHP security update is incomplete. Server files were not updated.${NC}\n"
    exit 1
fi

printf ">> Checking installed server repository origins...\n"
/usr/local/bin/ddistro_server migrate-remotes all || exit 1

printf ">> Ensuring UTF-8 is the PostgreSQL default...\n"
update-locale LANG=C.UTF-8 LC_ALL=C.UTF-8
service postgresql start >/dev/null 7<&- || exit 1
/usr/local/bin/ensure_postgres_utf8 || exit 1

# Replace a shipped configuration file only while it still matches what an update installed.
# A file the user changed is kept; a missing file gets the shipped default.
SHIPPED_CONFIG_STATE=/var/lib/dwemerdistro/shipped-config
install_shipped_config() {
    local source="$1" target="$2" mode="$3"
    local baseline="$SHIPPED_CONFIG_STATE/$(basename "$target").sha256"
    local shipped current recorded=""

    shipped="$(sha256sum < "$source" | cut -d ' ' -f 1)" || return 1
    if [ -L "$target" ] || { [ -e "$target" ] && [ ! -f "$target" ]; }; then
        printf "${RED}[ERROR] $target is not a plain file. It was not changed.${NC}\n"
        return 1
    fi
    if [ -f "$target" ]; then
        current="$(sha256sum < "$target" | cut -d ' ' -f 1)" || return 1
        [ ! -f "$baseline" ] || recorded="$(head -n 1 "$baseline")"
        if [ "$current" != "$shipped" ] && [ "$current" != "$recorded" ]; then
            printf ">> Keeping customized %s\n" "$target"
            return 0
        fi
    fi
    install -d -o root -g root -m 755 "$SHIPPED_CONFIG_STATE" \
        && install -o root -g root -m "$mode" "$source" "$target" \
        && printf '%s\n' "$shipped" > "$baseline"
}

printf ">> Copying configuration files to /etc...\n"
find /home/dwemer/dwemerdistro/etc/ -type f ! -name "php.ini" ! -name "dwemerdistro_services.conf" \
    ! -name "dwemerdistro-lmstudio.json" ! -name "cuda_config" -exec cp {} /etc/ \; 2>/dev/null
if [ $? -eq 0 ]; then
    # The LM Studio helper runs as dwemer and must read the public model catalog.
    install_shipped_config /home/dwemer/dwemerdistro/etc/dwemerdistro-lmstudio.json /etc/dwemerdistro-lmstudio.json 644 || exit 1
    install_shipped_config /home/dwemer/dwemerdistro/etc/cuda_config /etc/cuda_config 644 || exit 1
    # The validated shared component catalog is shipped with the Assistant.
    install -o root -g root -m 644 /home/dwemer/dwemerdistro/etc/dwemerdistro-components.json /etc/dwemerdistro-components.json || exit 1
    # Keep the existing service ports, including CUSTOM_MODS_PORT; only a missing file gets the defaults.
    # start_env sources it as root, so an existing file must be a plain root-owned file only root can change.
    SERVICES_CONF=/etc/dwemerdistro_services.conf
    if [ ! -e "$SERVICES_CONF" ] && [ ! -L "$SERVICES_CONF" ]; then
        install -o root -g root -m 644 /home/dwemer/dwemerdistro/etc/dwemerdistro_services.conf "$SERVICES_CONF" || exit 1
    elif [ -L "$SERVICES_CONF" ] || [ ! -f "$SERVICES_CONF" ] || [ "$(stat -c '%u' "$SERVICES_CONF")" != 0 ] \
        || [ $(( 0$(stat -c '%a' "$SERVICES_CONF") & 022 )) -ne 0 ]; then
        printf "${RED}[ERROR] $SERVICES_CONF must be a plain root-owned file that only root can change. It was not changed.${NC}\n"
        exit 1
    fi
    if ! runuser -u dwemer -- python3 -c 'import json, sys; sys.exit(not isinstance(json.load(open(sys.argv[1]))["models"], list))' /etc/dwemerdistro-lmstudio.json; then
        printf "${RED}[ERROR] LM Studio model catalog is not readable by dwemer${NC}\n"
        exit 1
    fi
    if ! runuser -u dwemer -- python3 -c 'import json, sys; sys.exit(not isinstance(json.load(open(sys.argv[1]))["components"], list))' /etc/dwemerdistro-components.json; then
        printf "${RED}[ERROR] Component catalog is not readable by dwemer${NC}\n"
        exit 1
    fi
    printf "${GREEN}[SUCCESS] Configuration files successfully copied to /etc/${NC}\n"
else
    printf "${RED}[ERROR] Error copying configuration files to /etc/${NC}\n"
    exit 1
fi

print_header "UPDATING PHP CONFIGURATION"

# Only a freshly prepared image has the marker that enables this initialization.
/usr/local/bin/initialize_fresh_distro || exit 1

# Existing PHP settings belong to the user; only a missing Apache php.ini gets the distro default.
printf ">> Checking PHP Apache configuration...\n"
PHP_APACHE_INI=/etc/php/8.2/apache2/php.ini
if [ -e "$PHP_APACHE_INI" ] || [ -L "$PHP_APACHE_INI" ]; then
    printf "${GREEN}[SUCCESS] Existing PHP configuration kept unchanged${NC}\n"
elif install -o root -g root -m 644 /home/dwemer/dwemerdistro/etc/php.ini "$PHP_APACHE_INI"; then
    printf "${GREEN}[SUCCESS] Default PHP configuration installed${NC}\n"
else
    printf "${RED}[ERROR] Error installing the default PHP configuration${NC}\n"
    exit 1
fi

# Existing and fresh users receive the same hash-verified runtime, without account sign-in.
# An interrupted download can be retried; no user data or provider credentials are replaced.
/usr/local/bin/install_assistant_runtime --install-prerequisites 7<&- || exit 1
# Do not report a successful Assistant upgrade with a skipped or failed isolation check.
/usr/local/bin/ddistro --json agent probe 7<&- || exit 1

printf "\n${BOLD}=======================================================${NC}\n"
printf "${BOLD}${GREEN}        CORE SYSTEM UPDATED SUCCESSFULLY!          ${NC}\n"
printf "${BOLD}=======================================================${NC}\n\n"

#DO NOT REMOVE Begin CHIM Server Update as this triggers the update_gws step in the CHIM launcher.
echo "Begin CHIM Server Update"
