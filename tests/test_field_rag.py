from __future__ import annotations

from pathlib import Path

from mth.rag import FieldPack


def test_field_pack_loads_drop_in_markdown_and_filters_device_model(tmp_path: Path) -> None:
    recipes = tmp_path / "field-recipes"
    recipes.mkdir()
    (recipes / "sxtsq.md").write_text(
        """---
kind: field_recipe
collection: rag2b_field
id: sxtsq
device_models: [SXTsq Lite5, RBSXTsq5nD]
keywords: [subscriber CPE, station bridge]
---
# SXTsq Lite5 subscriber CPE
Use the approved station bridge and PPPoE VLAN recipe.
""",
        encoding="utf-8",
    )
    (recipes / "notes.md").write_text("# Not a field recipe\n", encoding="utf-8")
    (recipes / "broken.md").write_text("---\nkind: field_recipe\n", encoding="utf-8")

    pack = FieldPack.load(recipes)

    found = pack.search("station bridge", device_model="RBSXTsq5nD")
    assert [recipe.recipe_id for recipe in found] == ["sxtsq"]
    assert pack.search("station bridge", device_model="CHR") == ()
    assert pack.invalid_files == ("broken.md",)
