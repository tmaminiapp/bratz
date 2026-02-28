#!/bin/bash
echo "🚀 Запуск BRATZ в локальном режиме..."
echo "==================================="

# Проверяем наличие Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 не найден! Установите Python 3.8 или выше"
    exit 1
fi

# Устанавливаем зависимости
echo "📦 Проверка зависимостей..."
pip3 install python-dotenv python-telegram-bot firebase-admin --quiet

# Запускаем бота
export ENVIRONMENT=local
python3 bratz.py
