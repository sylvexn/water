#!/bin/bash
# Install WaterH collector on fridge
set -e

sudo mkdir -p /opt/waterh
sudo cp collector.py /opt/waterh/
sudo chown -R syl:syl /opt/waterh

# Create .env if it doesn't exist
if [ ! -f /opt/waterh/.env ]; then
    echo "WATERH_API_TOKEN=changeme" | sudo tee /opt/waterh/.env > /dev/null
    sudo chmod 600 /opt/waterh/.env
    echo ">>> Edit /opt/waterh/.env and set WATERH_API_TOKEN"
fi

sudo cp waterh.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable waterh
sudo systemctl start waterh

echo ">>> Installed. Check status: systemctl status waterh"
echo ">>> Logs: journalctl -u waterh -f"
