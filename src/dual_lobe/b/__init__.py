"""Lobe B support modules.

Submodules are imported explicitly by their consumers. Eagerly importing the
worker here creates an API/state import cycle when prompt utilities are used.
"""
