#!/usr/bin/env python3
"""BillSense v2 — AI-Heavy Electricity Bill Analyser"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from app import app, init_db

if __name__ == "__main__":
    print("\n╔═══════════════════════════════════════════════════════╗")
    print("║  ⚡  BillSense v2 — AI Electricity Bill Analyser      ║")
    print("╚═══════════════════════════════════════════════════════╝")
    init_db()
    app.run(debug=False, host="0.0.0.0", port=5000)
