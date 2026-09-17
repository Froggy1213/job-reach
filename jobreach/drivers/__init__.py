"""Fetch drivers — small scripts executed by *another* interpreter.

A driver exists so the plugin can use a heavy dependency without importing it:
the engine stays standard-library-only, and the driver runs in whatever
interpreter actually has that dependency installed. ``scrapling_driver.py`` is
the only one today.
"""
