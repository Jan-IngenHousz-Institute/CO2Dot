"""
main.py — Entry point for the CO2Dot GUI.

Usage:
    python main.py

Requirements:
    pip install -r requirements.txt
"""

import sys
import os

# Ensure imports resolve correctly when run from any working directory
sys.path.insert(0, os.path.dirname(__file__))

from PySide6.QtWidgets import QApplication
from main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("CO2Dot Controller")
    app.setOrganizationName("JII")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
