"""Запуск для разработки (нужен NATS на localhost:4222): python main.py"""
import sys
from pathlib import Path

from potehinsonnet.run import main

if __name__ == "__main__":
    sys.exit(main([str(Path(__file__).resolve().parent)]))
