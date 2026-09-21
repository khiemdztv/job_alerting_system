"""Build a code update from validated Linux dependencies; never deploy or send messages."""

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(base: Path, output: Path) -> int:
    if base.resolve() == output.resolve():
        raise ValueError("Keep the original package for rollback; output must be different")
    with zipfile.ZipFile(base) as source:
        names = source.namelist()
        native = [
            name
            for name in names
            if name.startswith("pydantic_core/_pydantic_core.") and name.endswith(".so")
        ]
        if not any("cpython-312-x86_64-linux-gnu" in name for name in native):
            raise ValueError("Base ZIP needs the Linux x86_64 CPython 3.12 pydantic_core wheel")
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for name in names:
                normalized = name.replace("\\", "/")
                if normalized.startswith(("src/", "lambdas/")) or "__pycache__" in normalized:
                    continue
                if normalized.startswith("/") or ".." in normalized.split("/"):
                    raise ValueError("Unsafe archive path")
                target.writestr(normalized, source.read(name))
            count = 0
            for directory in ("src", "lambdas"):
                for path in sorted((ROOT / directory).rglob("*.py")):
                    if "__pycache__" not in path.parts:
                        compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
                        target.write(path, path.relative_to(ROOT).as_posix())
                        count += 1
    print(f"Built {output}: {count} Python files, {output.stat().st_size:,} bytes")
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=ROOT / "dist/deployment.zip")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/deployment-reviewed.zip")
    args = parser.parse_args()
    build(args.base, args.output)
