"""MkDocs Drawio Plugin"""

from __future__ import annotations

import json
import logging
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Optional, cast

from bs4 import BeautifulSoup
from lxml import etree
from mkdocs.config import base
from mkdocs.config import config_options as c
from mkdocs.exceptions import ConfigurationError
from mkdocs.plugins import BasePlugin
from mkdocs.utils import copy_file

if TYPE_CHECKING:
    from mkdocs.config.defaults import MkDocsConfig
    from mkdocs.structure.pages import Page


IMG_RE = re.compile(r"""
    (!\[[^\]]*]                # ![alt]
     \(                        # (
       (?P<src>[^)\s]+         #   src
          \.drawio(?:\.svg)?   #   .drawio ou .drawio.svg
       )
       (?:\s+"[^"]*")?         #   "title" (optionnel)
     \)                        # )
    )
    (?P<attrs>\s*\{[^}]*\})?   # { ... } attributs (optionnels)
""", re.VERBOSE)

LOGGER = logging.getLogger("mkdocs.plugins.drawio")

def _add_classes_to_match(m, classes):
    class_str = " ".join(f".{c}" for c in classes)
    if m.group("attrs"):
        # On insère avant la '}' de fin
        attrs = m.group("attrs").rstrip("}")
        return f"{m.group(1)}{attrs} {class_str}}}"
    else:
        return f"{m.group(1)}{{ {class_str} }}"

class DrawioConfig(base.Config):
    """Configuration options for the Drawio Plugin"""

    # Viewer is required to convert the XML to SVG in the browser
    # Default to the online viewer, but can be overridden with a local copy
    # Be careful, new drawio versions may break compatibility if the viewer
    # is not updated accordingly.
    viewer_js = c.Type(
        str, default="https://viewer.diagrams.net/js/viewer-static.min.js"
    )

    # Allow hovering toolbar for zoom and pan. Typical options are
    # pages zoom layers lightbox
    toolbar = c.Type(str, default="")

    # Show tooltips when hovering over diagram elements
    tooltips = c.Type(bool, default=False)

    # Border width around the diagram in pixels
    border = c.Type(int, default=5)

    # Allow editing the diagram in a new lightbox window
    edit = c.Type(bool, default=False)

    # Choose the page number to display from the diagram
    use_page_attribute = c.Type(bool, default=False)


class DrawioPlugin(BasePlugin[DrawioConfig]):
    """
    Plugin for embedding Drawio Diagrams into your MkDocs
    """

    def __init__(self) -> None:
        super().__init__()
        self.base = Path(__file__).parent
        self.css = []
        self.js = []

        self.attr_list_enabled = False
        self._remote_viewer_url: Optional[str] = None
        self._viewer_local_rel: str = "js/viewer-static.min.js"

    def on_page_markdown(self, markdown: str, /, **_kwargs) -> str | None:
        """Add class to images with drawio extension for preventing glightbox to
        patch them."""
        if not self.attr_list_enabled:
            return markdown
        return IMG_RE.sub(lambda m: _add_classes_to_match(m, ["off-glb"]), markdown)

    def on_post_page(
        self, output: str, /, *, page: Page, config: MkDocsConfig
    ) -> str | None:
        """Replace images with drawio extension with embedded diagrams"""


        if ".drawio" not in output.lower():
            return output

        soup = BeautifulSoup(output, "html.parser")

        diagram_config = {
            "toolbar": self.config.toolbar if self.config.toolbar else None,
            "tooltips": "1" if self.config.tooltips else "0",
            "border": self.config.border,
            "resize": "1",
            "lightbox": 1,
            "appearance": "automatic"
        }
        if self.config.edit:
            diagram_config["edit"] = "_blank"

        # diagram_config["toolbar"] = "pages zoom layers lightbox"

        # search for images using drawio extension
        diagrams = soup.find_all(
            "img", src=re.compile(r"\.drawio(?:\.svg)?(?:$|\?)", re.IGNORECASE)
        )
        if len(diagrams) == 0:
            return output

        style_val = "max-width:100%;border:1px solid transparent;"

        for diagram in diagrams:
            src = cast(str, diagram["src"])

            diagram_page = cast(
                Optional[str],
                diagram.get("page" if self.config.use_page_attribute else "alt"),
            )

            cfg = {
                k: v
                for k, v in {
                    "toolbar": self.config.toolbar or None,
                    "tooltips": str(int(self.config.tooltips)),
                    "border": self.config.border,
                    "resize": "1",
                    "lightbox": 1,
                    "appearance": "automatic",
                    "url": src,
                    **({"edit": "_blank"} if self.config.edit else {}),
                    **({"page": diagram_page} if diagram_page else {}),
                }.items()
                if v is not None
            }

            mxgraph = soup.new_tag("div")
            mxgraph["class"] = "mxgraph"
            mxgraph["style"] = style_val
            mxgraph["data-mxgraph"] = json.dumps(cfg, separators=(",", ":"))

            diagram.replace_with(mxgraph)

        return str(soup)

    @staticmethod
    def parse_diagram(data, page, src="", path=None) -> str:
        """Extract page from diagram XML data"""
        if not page:
            return etree.tostring(data, encoding="unicode")

        try:
            mxfile_nodes = data.xpath("//mxfile")
            if not mxfile_nodes:
                LOGGER.error("Error: No <mxfile> root in '%s' (path '%s')", src, path)
                return ""
            mxfile = mxfile_nodes[0]

            pages = mxfile.xpath(f"./diagram[@name={json.dumps(page)}]")
            if not pages:
                LOGGER.warning(
                    "Warning: No page named '%s' in '%s' (path '%s')", page, src, path
                )
                return etree.tostring(mxfile, encoding="unicode")

            if len(pages) > 1:
                LOGGER.warning(
                    "Warning: Found multiple (%d) pages named '%s' "
                    "in '%s' (path '%s'); using first.",
                    len(pages),
                    page,
                    src,
                    path,
                )

            # Keep attributes from mxfile
            result = etree.Element(mxfile.tag, **mxfile.attrib)
            result.append(pages[0])
            return etree.tostring(result, encoding="unicode")

        except (etree.XPathEvalError, etree.XPathSyntaxError) as e:
            LOGGER.error(
                "XPath error parsing page '%s' in '%s' (path '%s'): %s",
                page,
                src,
                path,
                e,
            )

        except (TypeError, ValueError, AttributeError) as e:
            LOGGER.error(
                "Invalid XML structure for page '%s' in '%s' (path '%s'): %s",
                page,
                src,
                path,
                e,
            )

        return ""

    def on_config(self, config: MkDocsConfig) -> None:
        """Load embedded files"""
        self.attr_list_enabled = self._has_attr_list_extension(
            config.get("markdown_extensions", [])
        )

        # Check if plugin glightbox is enabled
        if 'glightbox' in config["plugins"] and not self.attr_list_enabled:
            raise ConfigurationError(
                "The markdown extension 'attr_list' must be enabled "
                "when the 'glightbox' plugin is used."
            )

        if not self.config.use_page_attribute and not self.attr_list_enabled:
            raise ConfigurationError(
                "The markdown extension 'attr_list' must be enabled "
                "when 'use_page_attribute' is enabled."
            )

        # Determine if we need to download the viewer JS
        viewer = self.config.viewer_js
        if isinstance(viewer, str) and viewer.startswith(("http://", "https://")):
            self._remote_viewer_url = viewer
            # Reference local path where we will save the downloaded file
            self.js.append(self._viewer_local_rel)
        else:
            # Assume it's a local path relative to the docs site
            self.js.append(viewer)

        # Mandatory for reloading diagrams when navigating with
        # Mkdocs-Material which has an observable event listener
        self.js.append("js/drawio-mkdocs.js")
        self.css.append("css/drawio-darkmode.css")

        for path in self.css:
            config.extra_css.append(str(path))
        for path in self.js:
            config.extra_javascript.append(str(path))

    def on_post_build(self, *, config: MkDocsConfig) -> None:
        """Copy embedded files to the site directory"""
        site = Path(config["site_dir"])

        # Copy embedded CSS and JS files
        for path in self.css + self.js:
            p = self.base / path
            if p.exists():
                copy_file(p, site / path)

        # Download the Drawio viewer JS if needed
        if self._remote_viewer_url:
            dest = site / self._viewer_local_rel
            dest.parent.mkdir(parents=True, exist_ok=True)

            try:
                req = urllib.request.Request(
                    self._remote_viewer_url,
                    headers={"User-Agent": "Mozilla/5.0 (MkDocs Drawio Plugin)"},
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                with open(dest, "wb") as f:
                    f.write(data)
                LOGGER.debug("Downloaded Drawio viewer to %s", dest)

            except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout) as e:
                LOGGER.warning(
                    "Could not download viewer from %s: %s. "
                    "Using runtime fallback to remote URL.",
                    self._remote_viewer_url,
                    e,
                )
                stub = (
                    "/*! drawio viewer fallback stub */\n"
                    "(function(){"
                    f"var s=document.createElement('script');"
                    f"s.src={json.dumps(self._remote_viewer_url)};"
                    "document.head.appendChild(s);"
                    "})();\n"
                )
                dest.write_text(stub, encoding="utf-8")
            except (OSError, PermissionError) as e:
                LOGGER.error("Filesystem error writing viewer to %s: %s", dest, e)
                raise

            except Exception as e:
                LOGGER.exception(
                    "Unexpected error while downloading Drawio viewer: %s", e
                )
                raise

    @staticmethod
    def _has_attr_list_extension(extensions) -> bool:
        for extension in extensions:
            name = DrawioPlugin._get_extension_name(extension)
            normalized = name.lower()
            if normalized in {
                "attr_list",
                "attrlist",
                "markdown.extensions.attr_list",
                "markdown.extensions.attrlist",
            }:
                return True
        return False

    @staticmethod
    def _get_extension_name(extension) -> str:
        if isinstance(extension, str):
            return extension
        if isinstance(extension, dict) and extension:
            return next(iter(extension.keys()))
        return ""
