#!/bin/sh
# A lancer une fois sur la VM Oracle (Ubuntu 24.04 arm64) : sudo sh setup_vm.sh
set -eu

apt-get update
apt-get install -y ca-certificates curl git

if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
fi
usermod -aG docker "${SUDO_USER:-ubuntu}"

if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

timedatectl set-timezone Europe/Paris

echo "OK. Deconnecte-toi puis reconnecte-toi en SSH pour que le groupe docker soit pris en compte."
