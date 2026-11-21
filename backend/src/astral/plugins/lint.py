"""Static review checks for plugin source (invariant 5).

'A plugin that imports a model library directly has hard-coded a hardware tier
and will be rejected in review' (04 §3, 05 §4). This makes that rejection
mechanical.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Top-level module names whose import means a plugin loads a model itself.
MODEL_LIBRARIES = frozenset({
    "torch", "torchvision", "torchaudio", "tensorflow", "keras", "jax", "onnxruntime",
    "onnx", "transformers", "sentence_transformers", "insightface", "ultralytics",
    "faster_whisper", "whisper", "llama_cpp", "ctranslate2", "openvino", "tensorrt",
    "mediapipe", "paddle", "paddleocr", "easyocr", "gliner", "spacy_transformers",
    "pyannote", "nemo", "vllm", "mlx",
})


def find_model_imports(source: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "__import__" and node.args
              and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            names = [node.args[0].value]
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "import_module" and node.args
              and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            names = [node.args[0].value]
        for n in names:
            if n.split(".")[0] in MODEL_LIBRARIES:
                found.append((node.lineno, n))
    return found


def lint_plugin_dir(path: Path) -> list[str]:
    problems = []
    for py in sorted(path.rglob("*.py")):
        for line, mod in find_model_imports(py.read_text(encoding="utf-8")):
            problems.append(
                f"{py.relative_to(path)}:{line}: imports model library {mod!r}; "
                "call a core capability (e.g. face.embed) instead"
            )
    return problems
