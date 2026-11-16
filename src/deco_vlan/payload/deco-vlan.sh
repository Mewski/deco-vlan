#!/bin/sh
# shellcheck shell=dash
#
# deco-vlan: carry a Deco's IoT and guest Wi-Fi networks on their own 802.1Q VLANs in access
# point mode.
#
# Runs on the Deco as root. One copy runs on every unit of the mesh and keeps this true for each
# network it has been given a VLAN ID for:
#
#   - a bridge (br-iot, br-guest) exists next to the stock LAN bridge (br-lan);
#   - every radio of that network is a member of its bridge instead of br-lan;
#   - the bridge reaches the rest of the mesh over the backhaul radios, tagged with the VLAN ID,
#     the same way the stock firmware carries the LAN tagged with VLAN 1;
#   - on the main unit the bridge reaches the wired network through the uplink port, tagged;
#   - guests can reach upstream and nothing else, unless the Deco app allows them local access.
#
# The stock firmware undoes parts of this whenever it reloads Wi-Fi or the network, so the
# script runs as a watcher and repairs the state once a second. Nothing here writes to flash
# except "install" and "uninstall".
#
# usage: deco-vlan.sh install | uninstall | boot | watch | stop | release [network...]

SELF_DIR=$(cd "$(dirname "$0")" && pwd)
INSTALL_DIR=/tp_data/user_data/deco-vlan
PIDFILE=/var/run/deco-vlan.pid
STATUS=/tmp/deco-vlan.status
WORK_MODE=/tmp/work_mode
LAN=br-lan
NETWORKS='iot guest'

# The configuration: a VLAN ID per network, empty for a network that is left alone.
IOT_VLAN='' GUEST_VLAN='' VERSION=unknown
# shellcheck source=/dev/null
[ -r "$SELF_DIR/config" ] && . "$SELF_DIR/config"

# What the watcher knows about the unit, looked up now and then.
TICK=0 ROLE='' MODE='' WIRED='' LAST=''
IOT_RADIOS='' GUEST_RADIOS='' GUEST_ISOLATE=1
children='' parents='' uplink=''

# The network the functions below work on, chosen with use().
BR='' VID='' RADIOS='' ISOLATE=0

log() { logger -t deco-vlan "$*"; }
exists() { [ -e "/sys/class/net/$1" ]; }
in_bridge() { [ -e "/sys/class/net/$1/brif/$2" ]; }
is_up() { [ $(($(cat "/sys/class/net/$1/flags") & 1)) = 1 ]; }

# ---------------------------------------------------------------------------- facts

# FAP is the main unit, RE a satellite. Empty until the mesh software has started.
role() { sed -n 's/.*"work_mode":"\([A-Z]*\)".*/\1/p' "$WORK_MODE" 2>/dev/null; }

# Names of the radios the firmware has configured for the given kinds of network, switched on or
# not. A radio shares its name with its section in the wireless configuration.
radios() {
    local kind
    for kind in "$@"; do
        uci -q show wireless | sed -n "s/^wireless\.\([^.]*\)\.type='$kind'$/\1/p"
    done | tr '\n' ' '
}

# Look up what changes rarely.
refresh() {
    ROLE=$(role)
    MODE=$(uci -q get sysmode.sysmode.mode)
    IOT_RADIOS=$(radios iot)
    GUEST_RADIOS=$(radios guest owe_guest)
    # "Allow Local Access" in the Deco app. Guests are kept apart unless it is switched on.
    GUEST_ISOLATE=1
    [ "$(uci -q get wifi.guest.host_isolation)" = 0 ] && GUEST_ISOLATE=0
}

# Make one network the subject of the functions below. $VID is empty if it is not configured.
use() {
    BR=br-$1
    case $1 in
        iot) VID=$IOT_VLAN RADIOS=$IOT_RADIOS ISOLATE=0 ;;
        guest) VID=$GUEST_VLAN RADIOS=$GUEST_RADIOS ISOLATE=$GUEST_ISOLATE ;;
    esac
}

# The backhaul radios as the mesh software sees them, one "<radio> <type> <broadcast>" per radio.
backhaul() {
    hyctl show 2>/dev/null | awk '{
        for (i = 1; i <= NF; i++) if ($i ~ /^ath[0-9]+\.[0-9]+$/) { sub(/\.[0-9]+$/, "", $i); print $i, $(i + 2), $(i + 4) }
    }'
}

# Sort the backhaul radios into $children, $parents and $uplink.
#
# Type "relay" faces children, "norelay" faces the parent. Several parent-facing radios can be
# associated at once; the mesh software sends broadcasts over exactly one of them, and that one
# is the uplink.
sort_backhaul() {
    children='' parents='' uplink=''
    # shellcheck disable=SC2046
    set -- $(backhaul)
    while [ $# -ge 3 ]; do
        case $2 in
            relay) children="$children $1" ;;
            norelay)
                parents="$parents $1"
                [ "$3" = enable ] && [ -z "$uplink" ] && uplink=$1
                ;;
        esac
        shift 3
    done
}

# Tagged or untagged member mask of a VLAN in the Ethernet switch, e.g. "0x20". Empty if absent.
switch_members() {
    ssdk_sh vlan entry show 2>/dev/null | awk -v v="[vid]:$1" -v k="[$2_member]:" '
        $1 == v { found = 1; next }
        found { for (i = 1; i <= NF; i++) if (index($i, k) == 1) { print substr($i, length(k) + 1); exit } }'
}

# Number of the lowest port set in a member mask.
port_of() {
    local mask=$(($1)) n=0
    [ "$mask" -gt 0 ] || return 1
    while [ $((mask & 1)) = 0 ]; do
        mask=$((mask >> 1))
        n=$((n + 1))
    done
    echo "$n"
}

# The main unit's wired uplink: the LAN bridge port behind which the default gateway is, e.g.
# "eth0.1". The stock firmware gives every Ethernet port its own VLAN towards the CPU, so the
# suffix identifies the physical port.
wired_uplink() {
    local gw mac no port
    gw=$(ip route | awk '$1 == "default" { print $3; exit }')
    [ -n "$gw" ] || return 1
    mac=$(awk -v ip="$gw" '$1 == ip { print tolower($4) }' /proc/net/arp)
    [ -n "$mac" ] || return 1
    no=$(brctl showmacs $LAN | awk -v m="$mac" '$2 == m { print $1; exit }')
    [ -n "$no" ] || return 1
    for port in "/sys/class/net/$LAN/brif"/eth*; do
        [ $(($(cat "$port/port_no"))) = "$no" ] && {
            echo "${port##*/}"
            return 0
        }
    done
    return 1
}

# -------------------------------------------------- actions on the network chosen with use()

bridge_up() {
    exists "$BR" && return 0
    brctl addbr "$BR" || return 1
    brctl stp "$BR" off
    brctl setfd "$BR" 0
    # The unit itself takes no address in the network.
    echo 1 >"/proc/sys/net/ipv6/conf/$BR/disable_ipv6"
    ifconfig "$BR" up
    log "created $BR for VLAN $VID"
}

# Put <device>.<VID> into the bridge, creating it if needed.
attach() {
    local dev=$1 port=$1.$VID
    exists "$dev" || return 1
    if ! exists "$port"; then
        vconfig add "$dev" "$VID" >/dev/null 2>&1 || return 1
        echo 1 >"/proc/sys/net/ipv6/conf/$port/disable_ipv6"
    fi
    if ! in_bridge "$BR" "$port"; then
        brctl addif "$BR" "$port" || return 1
        log "attached $port"
    fi
    is_up "$port" || ifconfig "$port" up
}

detach() {
    local port=$1.$VID
    exists "$port" || return 0
    vconfig rem "$port" >/dev/null 2>&1
    log "detached $port"
}

# Make the clients of a radio reconnect, so that they ask for an address in the network the
# radio now belongs to.
kick() {
    local radio=$1 ctrl sta
    ctrl=/var/run/hostapd-$(uci -q get "wireless.$radio.device")
    for sta in $(hostapd_cli -i "$radio" -p "$ctrl" list_sta 2>/dev/null); do
        hostapd_cli -i "$radio" -p "$ctrl" deauthenticate "$sta" >/dev/null 2>&1
    done
}

# Move a radio from one bridge to the other. A radio that is switched off does not exist.
move() {
    local radio=$1 from=$2 to=$3
    exists "$radio" || return 0
    in_bridge "$to" "$radio" && return 0
    in_bridge "$from" "$radio" && brctl delif "$from" "$radio"
    brctl addif "$to" "$radio" || return 1
    kick "$radio"
    log "moved $radio to $to"
}

# Main unit: let the VLAN through the Ethernet switch between the uplink port and the CPU, and
# bridge it. The switch drops tagged frames whose VLAN it does not know. $2 says whether to
# look at the switch in this pass.
wired() {
    local dev=${1%.*} vid=${1#*.} tagged untagged want
    if [ "$2" = 1 ]; then
        tagged=$(switch_members "$vid" tagged)
        untagged=$(switch_members "$vid" untagged)
        [ -n "$tagged" ] && [ -n "$untagged" ] || return 1
        want=$(printf '0x%x' $((tagged | untagged)))
        if [ "$(switch_members "$VID" tagged)" != "$want" ]; then
            ssdk_sh vlan entry del "$VID" >/dev/null 2>&1
            ssdk_sh vlan entry create "$VID" >/dev/null 2>&1
            ssdk_sh vlan member add "$VID" "$(port_of "$untagged")" tagged >/dev/null 2>&1
            ssdk_sh vlan member add "$VID" "$(port_of "$tagged")" tagged >/dev/null 2>&1
            log "switch: VLAN $VID tagged on port $(port_of "$untagged") and CPU port $(port_of "$tagged")"
        fi
        [ -n "$WIRED" ] && [ "${WIRED%.*}" != "$dev" ] && detach "${WIRED%.*}"
    fi
    attach "$dev"
}

# Go through the members of the bridge: drop those left over from another VLAN ID, and set the
# isolation flag. The kernel does not forward between two isolated ports, so with every port
# isolated except the uplink, a client reaches upstream and nothing else.
tidy() {
    local up=$1 path port want have
    for path in "/sys/class/net/$BR/brif"/*; do
        [ -e "$path" ] || continue
        port=${path##*/}
        case $port in
            *."$VID") ;;
            *.*)
                vconfig rem "$port" >/dev/null 2>&1
                log "dropped $port"
                continue
                ;;
        esac
        [ -e "$path/isolated" ] || continue
        want=$ISOLATE
        [ "$port" = "$up" ] && want=0
        read -r have <"$path/isolated"
        [ "$have" = "$want" ] || echo "$want" >"$path/isolated"
    done
}

# Undo everything for one network. Its radios go back into the LAN bridge.
release() {
    local radio path port
    for radio in $RADIOS; do move "$radio" "$BR" $LAN; done
    if exists "$BR"; then
        for path in "/sys/class/net/$BR/brif"/*; do
            [ -e "$path" ] || continue
            port=${path##*/}
            case $port in
                *.*) vconfig rem "$port" >/dev/null 2>&1 ;;
                *) brctl delif "$BR" "$port" ;;
            esac
        done
        ifconfig "$BR" down
        brctl delbr "$BR"
        log "removed $BR"
    fi
    [ "$ROLE" = FAP ] && [ -n "$VID" ] && [ -n "$(switch_members "$VID" tagged)" ] &&
        ssdk_sh vlan entry del "$VID" >/dev/null 2>&1
}

# ------------------------------------------------------------------------ the watcher

# One pass: bring the unit to the wanted state. Cheap when nothing has changed.
ensure() {
    local network radio eth='' check=0
    TICK=$((TICK + 1))
    # Every 30 s, and every second while the unit is still starting.
    if [ -z "$ROLE" ] || [ -z "$IOT_RADIOS$GUEST_RADIOS" ] || [ $((TICK % 30)) = 1 ]; then
        refresh
    fi
    if [ "$MODE" != AP ]; then
        # Router mode has its own handling of both networks.
        for network in $NETWORKS; do
            use "$network"
            exists "$BR" && release
        done
        report idle
        return
    fi

    children='' parents='' uplink=''
    if [ -n "$ROLE" ]; then
        sort_backhaul
        if [ "$ROLE" = FAP ]; then
            # The cable rarely moves to another port: look every 30 s, and until it is found.
            eth=$WIRED
            if [ -z "$WIRED" ] || [ $((TICK % 30)) = 1 ]; then
                eth=$(wired_uplink) || eth=$WIRED
                check=1
            fi
            uplink=${eth%.*}
        fi
    fi

    for network in $NETWORKS; do
        use "$network"
        if [ -z "$VID" ]; then
            exists "$BR" && release
            continue
        fi
        bridge_up || continue
        # Isolation first: the radios leave the LAN even while there is no path upstream.
        for radio in $RADIOS; do move "$radio" $LAN "$BR"; done
        if [ "$ROLE" = FAP ]; then
            [ -n "$eth" ] && wired "$eth" $check
        else
            # Exactly one path towards the parent, or the parallel links would form a loop.
            for radio in $parents; do [ "$radio" = "$uplink" ] || detach "$radio"; done
            [ -n "$uplink" ] && attach "$uplink"
        fi
        for radio in $children; do attach "$radio"; done
        tidy "${uplink:+$uplink.$VID}"
    done
    [ -n "$eth" ] && WIRED=$eth
    report "${ROLE:+active}"
}

# Publish the state in a file the installer can read without running anything: one line about
# the watcher, then one line per configured network.
report() {
    local network radio path list text line
    text="state=${1:-starting} role=$ROLE mode=$MODE uplink=$uplink"
    for network in $NETWORKS; do
        use "$network"
        [ -n "$VID" ] || continue
        text="$text
network=$network vlan=$VID isolated=$ISOLATE"
        list=''
        for radio in $RADIOS; do exists "$radio" && list="$list,$radio"; done
        text="$text radios=${list#,}"
        list=''
        for path in "/sys/class/net/$BR/brif"/*; do [ -e "$path" ] && list="$list,${path##*/}"; done
        text="$text members=${list#,}"
    done
    [ "$text" = "$LAST" ] && [ $((TICK % 30)) != 0 ] && return
    LAST=$text
    read -r line </proc/uptime
    echo "version=$VERSION pid=$$ uptime=${line%% *} $text" >$STATUS.tmp && mv $STATUS.tmp $STATUS
}

running() { [ -r $PIDFILE ] && grep -qs deco-vlan "/proc/$(cat $PIDFILE)/cmdline"; }

watch() {
    running && exit 0
    echo $$ >$PIDFILE
    # Two watchers started in the same moment both get here; the later one wins the file.
    sleep 1
    [ "$(cat $PIDFILE)" = $$ ] || exit 0
    trap 'exit 0' TERM INT
    log "watching (version $VERSION, IoT VLAN ${IOT_VLAN:-none}, guest VLAN ${GUEST_VLAN:-none})"
    while :; do
        ensure
        sleep 1
    done
}

# Stop the watcher and wait until it is gone, so that one started next cannot be mistaken for it.
stop() {
    local pid n=0
    running || return 0
    pid=$(cat $PIDFILE)
    kill "$pid"
    while [ -e "/proc/$pid" ] && [ $n -lt 10 ]; do
        sleep 1
        n=$((n + 1))
    done
    rm -f $PIDFILE $STATUS
}

# ------------------------------------------------------------------------- commands

# The VLAN ID a configuration file gives a network.
configured() { (
    IOT_VLAN='' GUEST_VLAN=''
    # shellcheck source=/dev/null
    . "$1"
    use "$2"
    echo "$VID"
); }

# Copy this directory to persistent storage and hook it into the firewall start-up, the one
# place where the stock firmware runs a file named in its saved configuration.
install() {
    local network old
    if [ "$SELF_DIR" != "$INSTALL_DIR" ] && [ -r $INSTALL_DIR/config ]; then
        sh $INSTALL_DIR/deco-vlan.sh stop
        # A network whose VLAN ID changes is taken down by the installation that built it, so
        # that its clients ask for new addresses. Everything else is taken over as it stands.
        for network in $NETWORKS; do
            old=$(configured $INSTALL_DIR/config "$network")
            [ -n "$old" ] && [ "$old" != "$(configured "$SELF_DIR/config" "$network")" ] &&
                sh $INSTALL_DIR/deco-vlan.sh release "$network"
        done
    fi
    mkdir -p $INSTALL_DIR || return 1
    if [ "$SELF_DIR" != "$INSTALL_DIR" ]; then
        cp "$SELF_DIR/deco-vlan.sh" "$SELF_DIR/boot.sh" "$SELF_DIR/config" $INSTALL_DIR/ || return 1
    fi
    if [ "$(uci -q get firewall.deco_vlan.path)" != "$INSTALL_DIR/boot.sh" ]; then
        uci set firewall.deco_vlan=include
        uci set firewall.deco_vlan.path=$INSTALL_DIR/boot.sh
        uci commit firewall
        saveconfig >/dev/null 2>&1
    fi
    sh $INSTALL_DIR/deco-vlan.sh stop
    sh $INSTALL_DIR/deco-vlan.sh boot
}

# Stop the watcher and undo the given networks, or all of them.
release_networks() {
    local network
    # shellcheck disable=SC2086
    [ $# -gt 0 ] || set -- $NETWORKS
    stop
    refresh
    for network in "$@"; do
        use "$network"
        release
    done
}

uninstall() {
    release_networks
    if uci -q get firewall.deco_vlan >/dev/null; then
        uci delete firewall.deco_vlan
        uci commit firewall
        saveconfig >/dev/null 2>&1
    fi
    rm -rf $INSTALL_DIR $STATUS
}

main() {
    export PATH=/usr/sbin:/usr/bin:/sbin:/bin
    case $1 in
        # Called from the firewall start-up at boot and on every firewall reload.
        boot) (sh "$SELF_DIR/deco-vlan.sh" watch </dev/null >/dev/null 2>&1 &) ;;
        watch) watch ;;
        stop) stop ;;
        install) install ;;
        uninstall) uninstall ;;
        release)
            shift
            release_networks "$@"
            ;;
        *)
            echo "usage: $0 install|uninstall|boot|watch|stop|release [network...]" >&2
            return 2
            ;;
    esac
}

# The tests source this file to call single functions.
[ -n "${DECO_VLAN_LIB:-}" ] || main "$@"
