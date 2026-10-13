# pyright: reportArgumentType=none, reportOptionalMemberAccess=none, reportOptionalSubscript=none, reportCallIssue=none, reportAttributeAccessIssue=none, reportIndexIssue=none, reportGeneralTypeIssues=none, reportPossiblyUnboundVariable=none, reportUninitializedInstanceVariable=none, reportMissingImports=none, reportOperatorIssue=none, reportIncompatibleMethodOverride=none, reportAssignmentType=none, reportTypedDictNotRequiredAccess=none, reportPrivateLocalImportUsage=none, reportUnusedFunction=none, reportReturnType=none, reportIncompatibleVariableOverride=none
"""Generated viewers serve Chinese pages over HTTP without renaming management files."""

import functools
import http.server
import json
import threading
from urllib.parse import quote
from urllib.request import urlopen

import pytest

from codewiki.cli.html_generator import HTMLGenerator
from codewiki.src.be import doc_layout as L

TREE = {
    "身份认证": {
        "components": ["auth.py::Auth"],
        "children": {
            "会话": {
                "components": ["session.py::Session"],
                "children": {"存储": {"components": ["store.py::Store"], "children": {}}},
            }
        },
    },
    "SDK配置": {"components": ["config.py::Config"], "children": {}},
}


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def write_viewer(docs, layout="hierarchical"):
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "module_tree.json").write_text(json.dumps(TREE, ensure_ascii=False), encoding="utf-8")
    (docs / "metadata.json").write_text(
        json.dumps(
            {
                "generation_info": {
                    "language": "Chinese (Simplified)",
                    "layout": layout,
                }
            }
        ),
        encoding="utf-8",
    )
    paths = L.doc_relpaths(TREE, layout)
    for name, rel in paths.items():
        page = docs / rel
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            f"# {name}\n\n## 接口\n\n"
            f"[存储]({L.relative_link(rel, paths['存储'])}#接口)\n\n"
            f"[配置]({quote(L.relative_link(rel, paths['SDK配置']))})\n",
            encoding="utf-8",
        )
    HTMLGenerator().generate(output_path=docs / "index.html", title="中文验收", docs_dir=docs)
    return paths


@pytest.mark.parametrize("layout", ["hierarchical", "flat"])
def test_chinese_pages_and_viewer_over_http(tmp_path, layout):
    paths = write_viewer(tmp_path, layout)
    handler = functools.partial(QuietHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/index.html") as response:
            html = response.read().decode("utf-8")
        assert '<html lang="zh-CN">' in html
        assert '"存储": "存储"' in html
        assert json.dumps(paths["存储"], ensure_ascii=False) in html
        assert "{{DOC_PATHS_JSON}}" not in html
        for name, rel in paths.items():
            with urlopen(base + "/" + quote(rel)) as response:
                assert response.status == 200
                assert f"# {name}" in response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
