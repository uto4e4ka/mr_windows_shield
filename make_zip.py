"""Собрать zip этого плагина для установки через панель → dist/<имя>-<версия>.zip

    python make_zip.py                    # код и data/ (data/ ляжет на сервер только при первой установке)
    python make_zip.py --bump patch       # поднять версию (plugin.toml и pyproject.toml) и собрать
    python make_zip.py --no-data          # только код — для обновлений

Сама сборка — в библиотеке (potehinsonnet.pack): улучшения приходят вместе с ней.
"""
import sys
from pathlib import Path

from potehinsonnet.pack import main

if __name__ == "__main__":
    main([str(Path(__file__).resolve().parent), *sys.argv[1:]])
