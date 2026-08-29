"""Local, drop-in Markdown collection for reviewed field recipes."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from mth.core.mcp_client.runtime import project_root

FIELD_PACK_ENV = "MTH_FIELD_RAG_HOME"


@dataclass(frozen=True, slots=True)
class FieldRecipe:
    recipe_id: str
    title: str
    path: str
    text: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FieldPack:
    path: Path
    recipes: tuple[FieldRecipe, ...]
    invalid_files: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: str | Path | None = None) -> FieldPack:
        root = resolve_field_pack_dir(path)
        if not root.is_dir():
            return cls(root, ())
        recipes: list[FieldRecipe] = []
        invalid: list[str] = []
        for source in sorted(root.rglob("*.md")):
            try:
                if source.is_symlink():
                    raise ValueError("field recipe symlinks are not allowed")
                source.resolve().relative_to(root.resolve())
                recipe = _parse_recipe(source, root)
            except (OSError, TypeError, ValueError, yaml.YAMLError):
                invalid.append(str(source.relative_to(root)))
                continue
            if recipe is not None:
                recipes.append(recipe)
        return cls(root, tuple(recipes), tuple(invalid))

    def search(
        self,
        query: str,
        *,
        device_model: str = "",
        limit: int = 3,
    ) -> tuple[FieldRecipe, ...]:
        if limit < 1:
            return ()
        terms = _terms(query)
        if not terms:
            return ()
        ranked: list[tuple[int, FieldRecipe]] = []
        for recipe in self.recipes:
            models = _metadata_strings(recipe.metadata, "device_models")
            if device_model and models and not any(
                _model_matches(device_model, model) for model in models
            ):
                continue
            keywords = set(_terms(recipe.title)) | _terms(recipe.text[:4_000])
            keywords.update(_terms(" ".join(_metadata_strings(recipe.metadata, "keywords"))))
            score = len(terms & keywords)
            if score:
                ranked.append((score, recipe))
        ranked.sort(key=lambda item: (-item[0], item[1].recipe_id))
        return tuple(recipe for _, recipe in ranked[:limit])

    def has_trigger(self, query: str, *, device_model: str = "") -> bool:
        terms = _terms(query)
        for recipe in self.recipes:
            models = _metadata_strings(recipe.metadata, "device_models")
            if device_model and models and not any(
                _model_matches(device_model, model) for model in models
            ):
                continue
            triggers = _terms(" ".join(_metadata_strings(recipe.metadata, "trigger_terms")))
            if terms & triggers:
                return True
        return False


def resolve_field_pack_dir(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    configured = os.environ.get(FIELD_PACK_ENV)
    return (
        Path(configured).expanduser().resolve()
        if configured
        else project_root() / "docs" / "field-recipes"
    )


def _parse_recipe(source: Path, root: Path) -> FieldRecipe | None:
    raw = source.read_text(encoding="utf-8")
    metadata, body = _front_matter(raw)
    if metadata.get("kind") != "field_recipe" or metadata.get("collection") != "rag2b_field":
        return None
    recipe_id = metadata.get("id")
    if not isinstance(recipe_id, str) or not recipe_id.strip():
        raise ValueError("field recipe id is required")
    title_match = re.search(r"^#\s+(.+?)\s*$", body, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else recipe_id.strip()
    return FieldRecipe(
        recipe_id=recipe_id.strip(),
        title=title,
        path=str(source.relative_to(root)),
        text=body.strip(),
        metadata=metadata,
    )


def _front_matter(raw: str) -> tuple[dict[str, Any], str]:
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    end = normalized.find("\n---\n", 4)
    if end < 0:
        raise ValueError("unterminated field recipe front matter")
    parsed = yaml.safe_load(normalized[4:end]) or {}
    if not isinstance(parsed, dict):
        raise TypeError("field recipe front matter must be a mapping")
    return parsed, normalized[end + 6 :]


def _metadata_strings(metadata: dict[str, Any], key: str) -> tuple[str, ...]:
    value = metadata.get(key, ())
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _terms(value: str) -> set[str]:
    return {part.casefold() for part in re.findall(r"[^\W_]+", value, flags=re.UNICODE)}


def _model_matches(actual: str, expected: str) -> bool:
    left = re.sub(r"[^a-z0-9]+", "", actual.casefold()).removeprefix("routerboard")
    right = re.sub(r"[^a-z0-9]+", "", expected.casefold()).removeprefix("routerboard")
    return bool(left and right and (left == right or left in right or right in left))
