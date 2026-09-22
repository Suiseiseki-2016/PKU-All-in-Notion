"""The localhost panel package (student-facing panel + engineering page).

`webapi`/`pipelines` import fastapi/uvicorn lazily inside the panel command,
so importing the package never pulls the web stack onto the default import
path. Only `pku-sync panel` (and the panel tests) import them.
"""
