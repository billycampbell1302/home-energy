#!/bin/bash
# Paste this into Oracle Cloud when creating the server:
#   Create instance -> Show advanced options -> Management -> "Paste cloud-init script"
# Change the password on the next line first (avoid the ' character).
export ADMIN_PASSWORD='CHANGE-ME-to-a-long-password'
export PEOPLE='Billy,Wife'
until curl -fsSL https://raw.githubusercontent.com/billycampbell1302/home-energy/main/deploy/install.sh -o /root/install.sh; do sleep 30; done
bash /root/install.sh
