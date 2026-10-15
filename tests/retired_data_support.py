"""Fixtures for preserving student data from retired versions."""
import json


def seed_old_home(root, value):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'notion-learning-home.json').write_text(json.dumps(value), 'utf-8')
