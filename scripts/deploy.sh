#!/bin/bash
# JARVIS — скрипт первоначального деплоя на VPS (Ubuntu 22.04)
# Запуск: bash deploy.sh

set -e

echo "=========================================="
echo "  JARVIS — Установка на VPS"
echo "=========================================="

# --- Системные пакеты ---
echo "[1/7] Установка системных пакетов..."
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip \
    postgresql-16 postgresql-client-16 \
    git curl wget ufw fail2ban

# --- Firewall ---
echo "[2/7] Настройка firewall..."
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
sudo ufw --force enable

# --- PostgreSQL ---
echo "[3/7] Настройка PostgreSQL..."
sudo -u postgres psql -c "CREATE USER jarvis WITH PASSWORD 'CHANGE_ME_PASSWORD';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE jarvis OWNER jarvis;" 2>/dev/null || true
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE jarvis TO jarvis;" 2>/dev/null || true

# --- Директории ---
echo "[4/7] Создание директорий..."
sudo mkdir -p /opt/jarvis
sudo mkdir -p /opt/jarvis/data/calls/incoming
sudo mkdir -p /opt/jarvis/data/calls/processed
sudo mkdir -p /opt/jarvis/data/cold
sudo mkdir -p /opt/jarvis/logs
sudo chown -R $USER:$USER /opt/jarvis

# --- Python venv ---
echo "[5/7] Создание Python venv..."
cd /opt/jarvis
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# --- Systemd сервис ---
echo "[6/7] Создание systemd сервиса..."
sudo tee /etc/systemd/system/jarvis.service > /dev/null <<EOF
[Unit]
Description=JARVIS Personal Assistant
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=$USER
WorkingDirectory=/opt/jarvis
ExecStart=/opt/jarvis/venv/bin/python main.py
Restart=always
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable jarvis

# --- Инструкция ---
echo "[7/7] Готово!"
echo ""
echo "=========================================="
echo "  JARVIS установлен!"
echo "=========================================="
echo ""
echo "Следующие шаги:"
echo "  1. Скопируй файлы проекта в /opt/jarvis/"
echo "  2. Создай /opt/jarvis/.env (из .env.example)"
echo "  3. Заполни TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN и т.д."
echo "  4. Измени пароль БД в .env (и в PostgreSQL: sudo -u postgres psql -c \"ALTER USER jarvis PASSWORD 'новый_пароль';\")"
echo "  5. Запусти: sudo systemctl start jarvis"
echo "  6. Проверь: sudo systemctl status jarvis"
echo "  7. Логи: journalctl -u jarvis -f"
echo ""
echo "Для Syncthing (записи звонков):"
echo "  sudo apt install syncthing"
echo "  systemctl --user enable syncthing"
echo "  systemctl --user start syncthing"
echo "  # Открой http://localhost:8384 для настройки"
echo ""
