from __future__ import annotations

import shutil
from http.client import RemoteDisconnected
from pathlib import Path

import pytest

from mth.rag import PackError, RagPack, load_or_build
from mth.rag.pack import fetch_url

INDEX_URL = "https://manual.example/llms.txt"
PAGES = {
    INDEX_URL: "# RouterOS\n- [IP addresses](ip-address.md)\n- [PPPoE](pppoe.md)\n",
    "https://manual.example/ip-address.md": """# IP addresses

Use `/ip address add address=192.0.2.1/24 interface=ether1` to add an address.

## Print

Use `/ip address print` to inspect configured addresses.
""",
    "https://manual.example/pppoe.md": """# PPPoE

The PPPoE client connects an interface to an access concentrator.
""",
}


def test_build_load_search_and_offline_reuse(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return PAGES[url]

    pack_path = tmp_path / "routeros-rag"
    pack = load_or_build(pack_path, index_url=INDEX_URL, fetcher=fetch, max_chunk_chars=256)

    assert pack.manifest["document_count"] == 2
    assert (pack_path / "sources" / "0001-ip-address.md").is_file()
    assert pack.search("configured addresses")[0].heading == "Print"
    assert calls == [INDEX_URL, *tuple(PAGES)[1:]]

    def forbidden_fetch(_url: str) -> str:
        raise AssertionError("a populated pack must never use the network")

    reused = load_or_build(pack_path, index_url=INDEX_URL, fetcher=forbidden_fetch)
    assert "PPPoE client" in reused.search("PPPoE client")[0].text


def test_pack_remains_valid_after_folder_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    load_or_build(source, index_url=INDEX_URL, fetcher=PAGES.__getitem__, max_chunk_chars=256)
    copied = tmp_path / "copied-to-offline-machine"
    shutil.copytree(source, copied)

    pack = RagPack.load(copied)

    assert pack.search("access concentrator")[0].source_url.endswith("pppoe.md")


def test_corrupt_non_empty_pack_is_rejected_without_rebuild(tmp_path: Path) -> None:
    pack_path = tmp_path / "routeros-rag"
    load_or_build(pack_path, index_url=INDEX_URL, fetcher=PAGES.__getitem__, max_chunk_chars=256)
    source = pack_path / "sources" / "0001-ip-address.md"
    source.write_text("tampered", encoding="utf-8")
    called = False

    def fetch(_url: str) -> str:
        nonlocal called
        called = True
        return ""

    with pytest.raises(PackError, match="checksum mismatch"):
        load_or_build(pack_path, index_url=INDEX_URL, fetcher=fetch)
    assert called is False


def test_fetch_url_retries_a_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b"ok"

    def open_url(*_args: object, **_kwargs: object) -> Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RemoteDisconnected("temporary disconnect")
        return Response()

    monkeypatch.setattr("mth.rag.pack.urlopen", open_url)
    monkeypatch.setattr("mth.rag.pack.time.sleep", lambda _seconds: None)

    assert fetch_url("https://manual.example/page.md") == b"ok"
    assert calls == 2


def test_lexical_retrieval_eval_prefers_exact_topics_then_falls_back(tmp_path: Path) -> None:
    index_url = "https://manual.example/llms.txt"
    pages = {
        index_url: (
            "- [Safe Mode](safe-mode.md)\n"
            "- [Bridge VLAN](bridge-vlan.md)\n"
            "- [Firewall NAT](firewall-nat.md)\n"
        ),
        "https://manual.example/safe-mode.md": (
            "# Safe Mode\nRouterOS Safe Mode rolls back changes after a session disconnect."
        ),
        "https://manual.example/bridge-vlan.md": (
            "# Bridge VLAN filtering\nConfigure tagged and untagged bridge VLAN ports."
        ),
        "https://manual.example/firewall-nat.md": (
            "# Firewall NAT\nUse a srcnat masquerade rule for a changing WAN address."
        ),
    }
    pack = load_or_build(
        tmp_path / "eval-rag",
        index_url=index_url,
        fetcher=pages.__getitem__,
        max_chunk_chars=256,
    )

    expected = {
        "safe mode rollback": "safe-mode.md",
        "bridge vlan filtering": "bridge-vlan.md",
        "firewall nat masquerade": "firewall-nat.md",
    }
    for query, suffix in expected.items():
        assert pack.search(query)[0].source_url.endswith(suffix)
    assert pack.search("missingword masquerade")[0].source_url.endswith("firewall-nat.md")
