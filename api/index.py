"""Vercel entry point. All routes are rewritten here (see vercel.json)."""
from leadapp.main import create_app

app = create_app()
