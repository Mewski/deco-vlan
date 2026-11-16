# shellcheck shell=dash
# Sourced by the stock firewall start-up through the "deco_vlan" include in /etc/config/firewall,
# at boot and on every firewall reload. It only makes sure the watcher is running.
sh /tp_data/user_data/deco-vlan/deco-vlan.sh boot
