"""Sphinx configuration for ``protea-sources``."""

from __future__ import annotations

import os
import sys
from importlib.metadata import version as _pkg_version

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, ROOT)

project = "protea-sources"
author = "Francisco Miguel Pérez Canales"
copyright = "2026, Francisco Miguel Pérez Canales"

try:
    release = _pkg_version("protea-sources")
except Exception:
    release = "0.0.1"
version = release

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx_copybutton",
    "sphinx_design",
]

# protea-contracts is a sibling package that may not be installed in the
# docs build environment. Mock it so autodoc can import the source modules
# without a live installation of the contracts package.
autodoc_mock_imports = [
    "protea_contracts",
    "pydantic",
    "requests",
]

autosummary_generate = True
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
    "member-order": "bysource",
    "exclude-members": "__weakref__,__init_subclass__,__subclasshook__",
}
autodoc_typehints = "description"
napoleon_google_docstring = True
napoleon_numpy_docstring = True

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "requests": ("https://requests.readthedocs.io/en/latest/", None),
}

html_theme = "shibuya"
html_title = f"{project} {release}"
html_theme_options = {
    "github_url": "https://github.com/frapercan/protea-sources",
    "nav_links": [
        {"title": "PROTEA platform", "url": "https://github.com/frapercan/PROTEA"},
        {"title": "Docs", "url": "https://protea-sources.readthedocs.io"},
    ],
}
html_static_path: list[str] = []

templates_path = ["_templates"]
exclude_patterns: list[str] = ["_build", "Thumbs.db", ".DS_Store"]

master_doc = "index"
